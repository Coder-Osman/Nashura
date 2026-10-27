"""Extract an audited IV into private output; never publish source literals or keys."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from preference_method_audit import HEADER, METHODS, method_evidence, sha256


def extract(text, source_sha256):
    active = False
    values = []
    for line in text.splitlines():
        header = HEADER.fullmatch(line)
        if header:
            active = header[1] == METHODS[0]
        if active and "const-string " in line:
            match = re.search(r'const-string v\d+, ("(?:[^"\\]|\\.)*") // string@[0-9a-f]+$', line)
            if not match:
                raise ValueError("unsupported literal representation")
            value = json.loads(match[1])
            if sha256(value.encode("utf-8")) == source_sha256:
                values.append(value)
        if line.strip() == "positions     :":
            active = False
    if len(values) != 1 or not values[0].isascii() or len(values[0]) < 16:
        raise ValueError("IV literal missing, ambiguous or outside audited ASCII scope")
    return values[0].encode("ascii")[:16]


def main():
    parser = argparse.ArgumentParser()
    for name in ("dex", "dexdump", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("dex-sha256", "dexdump-sha256", "constructor-sha256", "iv-source-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    dex, tool = args.dex.read_bytes(), args.dexdump.read_bytes()
    if sha256(dex) != args.dex_sha256 or sha256(tool) != args.dexdump_sha256:
        raise ValueError("input identity mismatch")
    if args.output.exists():
        raise ValueError("exclusive private output required")
    result = subprocess.run([str(args.dexdump), "-d", str(args.dex)], capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError("dexdump failed; diagnostics not published")
    text = result.stdout.decode("utf-8")
    methods = method_evidence(text, METHODS[:2], dex)
    if methods[METHODS[0]]["sha256"] != args.constructor_sha256:
        raise ValueError("audited constructor identity mismatch")
    iv = extract(text, args.iv_source_sha256)
    if args.dex.read_bytes() != dex or args.dexdump.read_bytes() != tool:
        raise ValueError("input changed")
    args.output.mkdir(mode=0o700)
    descriptor = os.open(args.output / "iv.bin", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(iv)
    report = {"schema": "nashura.preference-cipher-material.v1", "status": "STATIC_FOUND",
              "dex_sha256": args.dex_sha256, "dexdump_sha256": args.dexdump_sha256,
              "dump_sha256": sha256(result.stdout), "methods": methods,
              "iv_source_sha256": args.iv_source_sha256, "iv_sha256": sha256(iv),
              "iv_bytes": len(iv), "input_unchanged": True,
              "key_identity": "UNRESOLVED", "android_cipher_parity": "NOT_RUN"}
    with (args.output / "result.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
