"""Bind historical store0 material to an explicit observed source; never acquire identity."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from preference_cipher_material import extract
from preference_identity_material import private_write
from preference_initializer_audit import initializer_graph
from preference_method_audit import METHODS, method_evidence, sha256


def historical_key(document):
    if (document.get("schema") != "e2.1j-derivation-matrix.v1" or
            document.get("initialization_replaced") is not False or
            document.get("original_xml_writer_replaced") is not False or
            document.get("restored") is not True or document.get("cleanup_errors") != [] or
            document.get("source_acquisition") !=
            "Java retained; explicit transfer to native after complete trajectory"):
        raise ValueError("reviewed unmodified historical acquisition required")
    source = document.get("source")
    if (not isinstance(source, dict) or set(source) != {"source", "identity", "package"} or
            source["source"] != "original-app-context Settings.Secure" or
            not isinstance(source["identity"], str) or
            not re.fullmatch(r"[0-9a-f]{16}", source["identity"]) or
            not isinstance(source["package"], str) or
            not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", source["package"])):
        raise ValueError("explicit observed historical identity and package required")
    return hashlib.sha256(source["identity"].encode("ascii")).digest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dex", "dexdump", "reference"):
        parser.add_argument("--" + name, required=True, type=Path)
        parser.add_argument("--" + name + "-sha256", required=True)
    for name in ("graph-sha256", "constructor-sha256", "iv-source-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    inputs = {name: getattr(args, name).read_bytes() for name in ("dex", "dexdump", "reference")}
    if (args.output.exists() or any(sha256(data) != getattr(args, name + "_sha256")
                                    for name, data in inputs.items())):
        raise ValueError("input mismatch or existing output")
    result = subprocess.run([str(args.dexdump), "-d", str(args.dex)], capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError("dump failed; diagnostics stay private")
    text = result.stdout.decode("utf-8")
    graph = initializer_graph(text, inputs["dex"])
    constructor = method_evidence(text, METHODS[:1], inputs["dex"])[METHODS[0]]
    if graph["sha256"] != args.graph_sha256 or constructor["sha256"] != args.constructor_sha256:
        raise ValueError("reviewed control flow differs")
    key = historical_key(json.loads(inputs["reference"]))
    iv = extract(text, args.iv_source_sha256)
    if any(getattr(args, name).read_bytes() != raw for name, raw in inputs.items()):
        raise ValueError("input changed")
    args.output.mkdir(mode=0o700)
    private_write(args.output / "o-key.bin", key)
    private_write(args.output / "iv.bin", iv)
    report = {"schema": "nashura.preference-historical-identity.v1", "status": "STATIC_FOUND",
              "reference_sha256": args.reference_sha256, "dex_sha256": args.dex_sha256,
              "dexdump_sha256": args.dexdump_sha256, "dump_sha256": sha256(result.stdout),
              "dump_exit_code": result.returncode, "initializer_graph": graph,
              "constructor": constructor, "private_output_sizes": [32, 16],
              "scope": "historical observed store0 identity only; no identity acquisition",
              "current_runtime_binding": "UNRESOLVED", "storage_and_backup": "UNRESOLVED",
              "runtime_parity": "NOT_RUN", "inputs_unchanged": True}
    private_write(args.output / "result.json", (json.dumps(report, indent=2) + "\n").encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
