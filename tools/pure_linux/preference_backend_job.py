"""Compare historical ASCII codec references and XML persistence in isolated Bionic children."""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import xml.etree.ElementTree as ET

from native_init_job import check_inputs, digest, run
from preference_cipher_job import read_evidence, validate_mappings


def entries(path):
    root = ET.fromstring(path.read_bytes())
    if root.tag != "map" or root.attrib:
        raise ValueError("string map required")
    output = {}
    for child in root:
        if child.tag != "string" or set(child.attrib) != {"name"} or len(child):
            raise ValueError("string entry required")
        key = child.attrib["name"]
        if key in output:
            raise ValueError("duplicate key")
        output[key] = child.text or ""
    return output


def validate(summary, mappings, identities):
    expected = {"status": "PASS", "historical_ascii_references": 48,
                "two_slot_commits": True, "reopen_read_equal": True, "backup_requests": 1,
                "backup_service": "UNRESOLVED", "decode_exceptions": 0,
                "libg_loaded": False, "catalog_ready_pass": False,
                "scope": "historical_ascii_codec_and_xml_persistence_only"}
    return (summary == expected and validate_mappings(mappings, identities) and
            not {"libart.so", "libandroid.so"}.intersection(row["name"] for row in mappings))


def compare_entries(original, actual):
    removed = original.keys() - actual.keys()
    changed = {key for key in original.keys() & actual.keys() if original[key] != actual[key]}
    added = actual.keys() - original.keys()
    passed = not removed and len(changed) + len(added) == 1
    return {"original_entries": len(original), "unrelated_entries_preserved": passed,
            "changed_existing_entries": len(changed), "added_entries": len(added),
            "removed_entries": len(removed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("payload", "probe", "material", "references", "xml0", "xml1", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("probe", "material", "references", "xml0", "xml1"):
        parser.add_argument("--" + name + "-sha256", required=True)
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise ValueError("native ARM64 Linux required")
    before = check_inputs(args.payload)
    files = {name: getattr(args, name).resolve() for name in ("probe", "material", "references", "xml0", "xml1")}
    expected = {name: getattr(args, name + "_sha256") for name in files}
    if any(digest(path) != expected[name] for name, path in files.items()):
        raise ValueError("experiment input identity differs")
    original = [entries(files["xml" + str(slot)]) for slot in range(2)]
    args.output.mkdir(exist_ok=False)
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("LD_", "NASHURA_"))}
    environment["LD_LIBRARY_PATH"] = str(args.payload / "libs")
    records = []
    for index in range(2):
        output = args.output / ("process-" + str(index))
        output.mkdir()
        store = output / "store"
        store.mkdir(mode=0o700)
        for slot in range(2):
            shutil.copyfile(files["xml" + str(slot)], store / ("store-" + str(slot) + ".xml"))
        result = run([str(args.payload / "linker64"), str(files["probe"]),
                      str(args.payload / "libs" / "libcrypto.so"), str(files["material"]),
                      str(files["references"]), str(store)], output, environment, 30)
        summary, mapped, errors = read_evidence(output)
        xmls, preserved = [], True
        for slot in range(2):
            path = store / ("store-" + str(slot) + ".xml")
            try:
                actual = entries(path)
                comparison = compare_entries(original[slot], actual)
                preserved = preserved and comparison["unrelated_entries_preserved"]
                xmls.append({"sha256": digest(path), **comparison})
            except (ValueError, OSError, ET.ParseError) as error:
                errors.append("xml:" + type(error).__name__)
                preserved = False
        result.update(assertion_summary=summary, mapped_files=mapped, evidence_errors=errors,
                      xmls=xmls, stdout_sha256=digest(output / "stdout.log"),
                      stderr_sha256=digest(output / "stderr.log"),
                      passed=result["exit_code"] == 0 and not result["timed_out"] and not errors and
                      preserved and validate(summary, mapped, {**before, "probe": expected["probe"]}))
        records.append(result)
    after = check_inputs(args.payload)
    unchanged = before == after and all(digest(path) == expected[name] for name, path in files.items())
    repeat = (records[0]["assertion_summary"] == records[1]["assertion_summary"] and
              records[0]["xmls"] == records[1]["xmls"])
    report = {"schema": "nashura.preference-backend-native.v1", "machine": platform.machine(),
              "kernel": platform.release(), "inputs_before": before, "inputs_after": after,
              "input_count": len(before), "experiment_inputs": expected, "inputs_unchanged": unchanged,
              "repeat_equal": repeat, "records": records, "current_runtime_identity": "UNRESOLVED",
              "android_xml_parity": "NOT_RUN", "base64_extended_domain": "UNRESOLVED",
              "backup_service": "UNRESOLVED", "libg_bridge_integration": "NOT_RUN",
              "catalog_ready_pass": False}
    with (args.output / "result.json").open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return int(not unchanged or not repeat or not all(row["passed"] for row in records))


if __name__ == "__main__":
    raise SystemExit(main())
