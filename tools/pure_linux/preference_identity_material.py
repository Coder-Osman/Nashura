"""Derive only the audited package-based identity; private bytes never enter reports."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess

from preference_cipher_material import extract
from preference_initializer_audit import initializer_graph
from preference_method_audit import HEADER, INITIALIZER, METHODS, method_evidence, sha256


def package_identity(package, table):
    if not isinstance(package, str) or not package or len(package) > 4096:
        raise ValueError("explicit bounded package required")
    if len(table) != 24 or any(type(value) is not int or not -(2 ** 31) <= value < 2 ** 31 for value in table):
        raise ValueError("24 signed int32 values required")
    units = package.encode("utf-16-le", errors="surrogatepass")
    characters = struct.unpack("<" + "H" * (len(units) // 2), units)
    return bytes(((characters[index] ^ table[index % 24]) & 31) + 48
                 for index in reversed(range(len(characters))))


def extract_table(text, dex):
    active = False
    lines, payloads = [], {}
    for line in text.splitlines():
        header = HEADER.fullmatch(line)
        if header:
            active = header[1] == INITIALIZER
        elif active and line.strip().startswith("catches"):
            active = False
        elif active:
            lines.append(line)
            match = re.fullmatch(r"([0-9a-f]+):[^|]*\|([0-9a-f]+): array-data \((\d+) units\)", line)
            if match:
                payloads[int(match[2], 16)] = (int(match[1], 16), int(match[3]))
    suffix = "\n".join(lines)
    slots = re.findall(r"iput-object .*Lcom/supercell/titan/GameApp;\.([ocp]):Lc/a;", suffix)
    if slots != ["o", "c", "p"]:
        raise ValueError("audited slot order differs")
    region = suffix.split("Lcom/supercell/titan/GameApp;.c:Lc/a;", 1)[1].split(
        "Lcom/supercell/titan/GameApp;.p:Lc/a;", 1)[0]
    references = re.findall(r"fill-array-data v\d+, ([0-9a-f]+) // [+-][0-9a-f]+", region)
    if len(references) != 1 or int(references[0], 16) not in payloads:
        raise ValueError("ambiguous identity table")
    offset, units = payloads[int(references[0], 16)]
    payload = dex[offset:offset + units * 2]
    if len(payload) != 104 or struct.unpack_from("<HHI", payload) != (0x300, 4, 24):
        raise ValueError("identity table layout differs")
    return list(struct.unpack_from("<24i", payload, 8)), sha256(payload)


def private_write(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def main():
    parser = argparse.ArgumentParser()
    for name in ("dex", "dexdump", "package-file", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("dex-sha256", "dexdump-sha256", "package-file-sha256", "graph-sha256",
                 "constructor-sha256", "iv-source-sha256"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    dex, tool, package_bytes = args.dex.read_bytes(), args.dexdump.read_bytes(), args.package_file.read_bytes()
    if (sha256(dex) != args.dex_sha256 or sha256(tool) != args.dexdump_sha256 or
            sha256(package_bytes) != args.package_file_sha256 or args.output.exists()):
        raise ValueError("identity mismatch or nonexclusive output")
    package = json.loads(package_bytes)
    if not isinstance(package, dict) or set(package) != {"package"}:
        raise ValueError("explicit package-only private document required")
    result = subprocess.run([str(args.dexdump), "-d", str(args.dex)], capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError("dump failed; diagnostics stay private")
    text = result.stdout.decode("utf-8")
    graph = initializer_graph(text, dex)
    constructor = method_evidence(text, METHODS[:1], dex)[METHODS[0]]
    if graph["sha256"] != args.graph_sha256 or constructor["sha256"] != args.constructor_sha256:
        raise ValueError("reviewed identity control flow differs")
    table, table_sha = extract_table(text, dex)
    identity = package_identity(package["package"], table)
    iv = extract(text, args.iv_source_sha256)
    if (args.dex.read_bytes() != dex or args.dexdump.read_bytes() != tool or
            args.package_file.read_bytes() != package_bytes):
        raise ValueError("input changed")
    args.output.mkdir(mode=0o700)
    private_write(args.output / "p-key.bin", hashlib.sha256(identity).digest())
    private_write(args.output / "iv.bin", iv)
    report = {"schema": "nashura.preference-identity-material.v1", "status": "STATIC_FOUND",
              "dex_sha256": args.dex_sha256, "dexdump_sha256": args.dexdump_sha256,
              "package_file_sha256": args.package_file_sha256, "dump_sha256": sha256(result.stdout),
              "dump_exit_code": result.returncode, "initializer_graph": graph,
              "constructor": constructor, "table_payload_sha256": table_sha,
              "slot_order": ["o", "c", "p"], "private_output_sizes": [32, 16],
              "package_runtime_binding": "UNRESOLVED", "o_device_identity": "UNRESOLVED",
              "default_charset": "UNRESOLVED", "default_locale": "UNRESOLVED",
              "scope": "reviewed p transform only; explicit package; no platform acquisition",
              "runtime_parity": "NOT_RUN", "input_unchanged": True}
    private_write(args.output / "result.json", (json.dumps(report, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
