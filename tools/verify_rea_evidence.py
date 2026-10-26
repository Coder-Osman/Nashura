"""Cross-check private REA exports without publishing binary-derived content."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def number(value):
    return value if isinstance(value, int) else int(value.removeprefix("ram:"), 16)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_offset(segments, address, size):
    matches = [segment for segment in segments if segment["type"] == "TYPE.LOAD"
        and segment["va"] <= address
        and address + size <= segment["va"] + segment["filesz"]]
    require(size > 0 and len(matches) == 1, "Non-unique file-backed ELF extent")
    segment = matches[0]
    return segment["offset"] + address - segment["va"]


def map_entry(segments, observations, digest, address, size):
    offset = source_offset(segments, address, size)
    matches = []
    for mapping in observations["mappings"]:
        index = mapping.get("source_file_index")
        sources = observations["source_files"]
        if (mapping.get("initialized") and mapping.get("file_offset") is not None
                and isinstance(index, int) and 0 <= index < len(sources)
                and sources[index]["original_sha256"] == digest
                and mapping["file_offset"] <= offset
                and offset + size <= mapping["file_offset"] + mapping["length"]):
            matches.append(mapping)
    require(len(matches) == 1, "Non-unique matching analysis source mapping")
    mapping = matches[0]
    return offset, number(mapping["start"]) + offset - mapping["file_offset"]


def file_within(root, relative):
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), "Evidence path escapes its root")
    return path


def read_json(path):
    return json.loads(path.read_text())


def validate_export(name, entry, row, result, analysis_address, digest, expected_addresses,
                    allow_extended):
    require(result.get("ok") is True, "Reported success has no successful export: " + name)
    evidence = result["value"]
    require(evidence["subject"]["digest"]["sha256"] == digest, "Dossier fingerprint mismatch")
    require(evidence["provider"]["id"] == "ghidra", "Unexpected provider")
    value = evidence["normalized_result"]
    require(number(value["procedure"]["address"]) == analysis_address,
        "Resolved neighboring function instead of exact entry: " + name)
    require(number(row["resolved_address"]) == analysis_address, "Summary entry mismatch")
    require(value["pseudocode"].strip() and value["assembly"], "Empty decompilation or assembly")
    body = value["procedure"]["body"]["ranges"]
    flow = value["native_value_flow"]
    require(flow["available"] is True, "High-pcode evidence unavailable")
    for operation in flow["operations"]:
        if operation.get("address"):
            require(any(number(part["start"]) <= number(operation["address"]) <= number(part["end"])
                for part in body), "Decompiler high-pcode escapes declared body: " + name)
    for facet in ("pseudocode", "assembly", "callers", "callees", "references"):
        require(value["facet_availability"][facet]["available"] is True,
            "Required evidence facet unavailable: " + facet)
    for field in ("callers", "callees", "incoming_references", "outgoing_references"):
        require(isinstance(value[field], list), "Missing relationship evidence: " + field)
    shift = analysis_address - entry["address"]
    observed = [number(line.split(":", 1)[0]) - shift for line in value["assembly"]]
    require(len(observed) == len(set(observed)), "Duplicate instruction addresses")
    require(observed[:len(expected_addresses)] == expected_addresses,
        "Instruction coverage is missing, reordered or has an internal gap: " + name)
    extended = observed != expected_addresses
    require(not extended or allow_extended, "Unapproved function boundary extension: " + name)
    expected_calls = {(edge["site"], edge["target"]) for edge in entry["callees"]
        if edge["kind"] == "call"}
    observed_calls = {(number(edge["source_address"]) - shift,
        number(edge["target_address"]) - shift) for edge in value["outgoing_references"]
        if edge["kind"].get("call") and edge["source_address"].startswith("0x")
        and edge["target_address"].startswith("0x")}
    require(expected_calls <= observed_calls, "Direct BL reference missing: " + name)
    return value, extended, len(observed) - len(expected_addresses)


def verify(static_root, export_root, binary, digest, allowed_failures=(), allowed_extensions=()):
    with binary.open("rb") as source:
        require(hashlib.file_digest(source, "sha256").hexdigest() == digest,
            "Local binary fingerprint mismatch")
    manifest = read_json(static_root / "manifest.json")
    require(manifest["binary_sha256"] == digest, "Static manifest fingerprint mismatch")
    require(manifest["architecture"] == "ARCH.AARCH64", "Unexpected static architecture")
    summary = read_json(export_root / "summary.json")
    require(summary["source_sha256"] == digest, "Summary fingerprint mismatch")
    image = read_json(export_root / "image.json")
    require(image.get("ok") is True, "Missing analysis load-image observations")
    observations = image["value"]["result"]["observations"]
    require(observations["language_id"].startswith("AARCH64:LE:64:"), "Wrong analysis language")
    require(any(source["original_sha256"] == digest for source in observations["source_files"]),
        "No matching original imported source")
    reference = read_json(static_root / "dossiers.json")
    names = [row["name"] for row in summary["rows"]]
    require(len(names) == len(set(names)) and set(names) == set(reference), "Dossier set mismatch")
    require(set(allowed_failures) <= set(names) and set(allowed_extensions) <= set(names),
        "Unknown exception name")
    counts = Counter()
    failures, extensions = [], []
    for row in summary["rows"]:
        name = row["name"]
        entry = reference[name]
        offset, address = map_entry(manifest["segments"], observations, digest,
            entry["address"], entry["size"])
        require(row["elf_address"] == entry["address"] and row["file_offset"] == offset
            and number(row["analysis_address"]) == address, "Summary source mapping mismatch")
        result = read_json(file_within(export_root, name + ".json"))
        if row["ok"] is False:
            require(result.get("ok") is False and result.get("error"), "Unsubstantiated failure")
            require(name in allowed_failures, "Unapproved failed entry: " + name)
            failures.append(name)
            continue
        require(row["ok"] is True, "Invalid success flag")
        expected = [number(line.split(":", 1)[0]) for line in
            file_within(static_root, entry["assembly"]).read_text().splitlines()]
        require(expected == list(range(entry["address"], entry["address"] + entry["size"], 4)),
            "Reference does not cover complete unwind extent")
        value, extended, extra = validate_export(name, entry, row, result, address, digest,
            expected, name in allowed_extensions)
        counts["successful_functions"] += 1
        counts["instructions"] += len(value["assembly"])
        counts["pseudocode_characters"] += len(value["pseudocode"])
        for field in ("incoming_references", "outgoing_references", "unresolved_calls"):
            counts[field] += len(value[field])
        if extended:
            extensions.append({"name": name, "extra_instructions": extra})
        else:
            counts["exact_instruction_extents"] += 1
    return {"schema": "nashura.rea-evidence-check.v1", "source_sha256": digest,
        "status": "verified_with_declared_limits" if failures or extensions else "verified",
        "counts": dict(counts), "failed_entries": failures, "extended_bodies": extensions,
        "load_image_status": image["value"]["result"].get("status"),
        "native_execution": "not_run", "vm_framework_free": "inconclusive"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-root", type=Path, required=True)
    parser.add_argument("--export-root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--allow-failed", action="append", default=[])
    parser.add_argument("--allow-extended", action="append", default=[])
    arguments = parser.parse_args()
    print(json.dumps(verify(arguments.static_root, arguments.export_root, arguments.binary,
        arguments.sha256, arguments.allow_failed, arguments.allow_extended), indent=2))


if __name__ == "__main__":
    main()
