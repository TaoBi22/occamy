#!/usr/bin/env python3
# Copyright 2026 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
"""Lower the AXI4-dialect crossbars written by occamygen into one SystemVerilog file."""

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys

STAMP = "// axi4-mlir-sha256: "


def split_top(s):
    """Split `s` at top-level commas."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(s):
        if c in "<({":
            depth += 1
        elif c in ">)}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(s[start:i])
            start = i + 1
    parts.append(s[start:])
    return parts


def module_body(hw_mlir, module):
    """Signature and body of `hw.module @module`."""
    m = re.search(r"^  hw\.module @{}\(.*?^  }}$".format(module), hw_mlir, re.S | re.M)
    return m.group(0)


def port_bits(body):
    """Total bit width of each port in a module signature."""
    start = body.index("(") + 1
    depth, end = 1, start
    while depth:
        depth += {"(": 1, ")": -1}.get(body[end], 0)
        end += 1
    bits = dict()
    for port in split_top(body[start:end - 1]):
        m = re.match(r"\s*(?:in %|out )(\w+)\s*:\s*(.*)", port, re.S)
        bits[m.group(1)] = sum(int(w) for w in re.findall(r"\bi(\d+)\b", m.group(2)))
    return bits


def port_order(body, prefix, struct, field, sig, pos):
    """Names of the `<prefix>*_<struct>` ports, ordered as the wrapper's `<field>K` ports."""
    names = dict()
    for m in re.finditer(r"^\s*(%.*?) = hw\.struct_explode %{}(\w+)_{}\b".format(prefix, struct),
                         body, re.M):
        names[m.group(1).split(", ")[pos]] = m.group(2)
    order = dict()
    for m in re.finditer(r"\b{}(\d+)_{}: (%\w+)".format(field, sig), body):
        order[int(m.group(1))] = names.get(m.group(2))
    return [order[k] for k in sorted(order)]


def check_ports(hw_mlir, expected):
    errors = []
    for module, exp in expected.items():
        body = module_body(hw_mlir, module)
        actual = port_bits(body)
        for port, bits in exp["bits"].items():
            if actual.get(port) != bits:
                errors.append("{}.{}: {} bits, but the connected struct has {}".format(
                    module, port, actual.get(port), bits))
        # The wrapper numbers ports as solder does, so the RTL can be compared index by index.
        if "inputs" not in exp:
            continue
        for kind, got in (("inputs", port_order(body, "in_", "req", "mgr", "awvalid", 1)),
                          ("outputs", port_order(body, "out_", "resp", "sub", "awready", 0))):
            if got != exp[kind]:
                errors.append("{}: wrapper orders {} as {}, expected {}".format(
                    module, kind, got, exp[kind]))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mlir", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--workdir", type=pathlib.Path, required=True)
    parser.add_argument("--circt-opt", default="circt-opt")
    parser.add_argument("--prebuilt", type=pathlib.Path,
                        help="Use this previously lowered file instead of running circt-opt.")
    args = parser.parse_args()

    mlir = args.mlir.read_text()
    ports = args.mlir.with_suffix(".json").read_text()
    stamp = STAMP + hashlib.sha256((mlir + ports).encode()).hexdigest() + "\n"
    ports = json.loads(ports)

    if args.prebuilt:
        sv = args.prebuilt.read_text()
        if not sv.startswith(stamp):
            sys.exit("{} was not lowered from the current {}; regenerate it with circt-opt.".format(
                args.prebuilt, args.mlir))
        args.out.write_text(sv)
        return

    shutil.rmtree(args.workdir, ignore_errors=True)
    args.workdir.mkdir(parents=True)

    # The dummies lowering takes one user width per network, so lower each width separately.
    groups = dict()
    for m in re.finditer(r"^hw\.module @(\w+)\(.*?^}$", mlir, re.S | re.M):
        groups.setdefault(ports[m.group(1)]["user_width"], []).append(m)

    sources = dict()
    for uw, modules in sorted(groups.items()):
        group_dir = args.workdir / "u{}".format(uw)
        group_dir.mkdir()
        group_mlir = group_dir / "dummies.mlir"
        group_mlir.write_text("\n".join(m.group(0) for m in modules) + "\n")
        hw_path = group_dir / "hw.mlir"
        subprocess.run([
            args.circt_opt, "--lower-axi4-dummies-to-axi=user-width={}".format(uw)
            if uw else "--lower-axi4-dummies-to-axi",
            "--lower-axi4-to-hw=pulp-mapping=true req-resp-ports=true", str(group_mlir), "-o",
            str(hw_path)
        ], check=True)

        errors = check_ports(hw_path.read_text(), {m.group(1): ports[m.group(1)] for m in modules})
        if errors:
            sys.exit("Generated crossbars do not match the ports Occamy connects:\n  " +
                     "\n  ".join(errors))

        sv_dir = group_dir / "sv"
        subprocess.run([
            args.circt_opt, "--test-apply-lowering-options=options=locationInfoStyle=none",
            "--lower-seq-to-sv", "--export-split-verilog=dir-name={}".format(sv_dir),
            str(hw_path), "-o", "/dev/null"
        ], check=True)

        for f in (sv_dir / "filelist.f").read_text().split():
            # Occamy's crossbars share one width each side, so a converter means a misderived width.
            if "converter" in f:
                sys.exit("Unexpected converter in the generated crossbars: " + f)
            text = (sv_dir / f).read_text()
            # Wrapper names do not encode the user width, so groups may emit the same name.
            if sources.setdefault(f, text) != text:
                sys.exit("Crossbars with different user widths both generate `{}`.".format(f))

    args.out.write_text(stamp + "".join(sources.values()))


if __name__ == "__main__":
    main()
