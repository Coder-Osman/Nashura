"""Audit a locally prepared baseline without loading any ELF or Android process."""
import argparse
import hashlib
import json
import struct
from pathlib import Path

from elf_audit import Elf64


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def member(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("manifest path outside baseline")
    candidate = root / path
    candidate.resolve().relative_to(root.resolve())
    return candidate


def verify_manifest(root, path):
    manifest = json.loads(path.read_text())
    records = []
    for entry in manifest["files"]:
        candidate = member(root, entry["path"])
        actual = sha256(candidate) if candidate.is_file() else None
        records.append({"path": entry["path"], "expected_sha256": entry["sha256"],
                        "actual_sha256": actual, "match": actual == entry["sha256"]})
    return {"manifest_sha256": sha256(path), "files": len(records),
            "matching": sum(entry["match"] for entry in records), "records": records}


def classify_candidates(root, candidates):
    records = {}
    compatible = []
    for path in candidates:
        with path.open("rb") as source:
            header = source.read(20)
        arm64 = (len(header) == 20 and header[:7] == b"\x7fELF\x02\x01\x01" and
                 struct.unpack_from("<H", header, 18)[0] == 183)
        records[str(path.relative_to(root))] = {
            "sha256": sha256(path), "aarch64_elf64": arm64}
        if arm64:
            compatible.append(path)
    return records, compatible


def audit(root, supported):
    libg = root / "app-libs/libg.so"
    if sha256(libg) != supported["libg_sha256"]:
        raise ValueError("libg identity mismatch; no binary analysis performed")
    engine = Elf64(libg.read_bytes()).report()
    if engine["machine"] != 183:
        raise ValueError("requires AArch64 engine")
    manifest_reports = {name: verify_manifest(root, root / name) for name in
                        ["app-manifest.json", "runtime-manifest.json", "resources-manifest.json"]}
    fingerprint = root / "resources/runtime-update/fingerprint.json"
    fingerprint_sha = sha256(fingerprint)
    libraries = {"app-libs/libg.so": engine}
    pending = [engine]
    resolutions = {}
    while pending:
        current = pending.pop()
        for name in current["needed"]:
            if Path(name).name != name:
                raise ValueError("DT_NEEDED contains a path; explicit loader policy required")
            if name in resolutions:
                continue
            candidates = sorted({path for path in [*(root / "app-libs").glob(name),
                                                   *(root / "sysroot").rglob(name)] if path.is_file()})
            identities, compatible = classify_candidates(root, candidates)
            unique = {identities[str(path.relative_to(root))]["sha256"] for path in compatible}
            state = "MISSING_INPUT" if not unique else "UNRESOLVED" if len(unique) > 1 else "STATIC_FOUND"
            resolutions[name] = {"status": state, "candidates": identities,
                                 "runtime_binding": "NOT_RUN", "namespace_policy": "UNRESOLVED"}
            if len(unique) == 1:
                chosen = compatible[0]
                relative = str(chosen.relative_to(root))
                if relative not in libraries:
                    parsed = Elf64(chosen.read_bytes()).report()
                    if parsed["machine"] != 183:
                        raise ValueError("non-AArch64 dependency")
                    libraries[relative] = parsed
                    pending.append(parsed)
    return {"schema": "nashura-pure-linux-input-audit.v1", "status": "STATIC_FOUND",
            "native_game_verified": False, "libg": engine, "manifests": manifest_reports,
            "fingerprint_sha256": fingerprint_sha,
            "fingerprint_match": fingerprint_sha == supported["runtime_fingerprint_sha256"],
            "libraries": libraries, "needed_candidates": resolutions,
            "runtime_load": "NOT_RUN", "relocation_and_symbol_versions": "UNRESOLVED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--supported-engine", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    report = audit(args.baseline, json.loads(args.supported_engine.read_text()))
    with args.output.open("x") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "libraries_inspected": len(report["libraries"]),
                      "fingerprint_match": report["fingerprint_match"],
                      "ambiguous_needed": [name for name, row in report["needed_candidates"].items()
                                           if row["status"] == "UNRESOLVED"]}))


if __name__ == "__main__":
    main()
