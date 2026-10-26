import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("rea_verifier",
    Path(__file__).resolve().parents[1] / "verify_rea_evidence.py")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


@pytest.fixture
def dataset(tmp_path):
    source = tmp_path / "synthetic.bin"
    source.write_bytes(b"synthetic-only")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    static = tmp_path / "static"
    exported = tmp_path / "exported"
    static.mkdir()
    exported.mkdir()
    manifest = {"binary_sha256": digest, "architecture": "ARCH.AARCH64",
        "segments": [{"type": "TYPE.LOAD", "va": 0x4000, "offset": 0, "filesz": 8}]}
    observations = {"language_id": "AARCH64:LE:64:v8A", "source_files": [
        {"original_sha256": digest}], "mappings": [{"initialized": True,
            "source_file_index": 0, "file_offset": 0, "length": 8, "start": "0x9000"}]}
    entry = {"address": 0x4000, "size": 8, "assembly": "entry.asm",
        "callees": [{"site": 0x4000, "target": 0x5000, "kind": "call"}]}
    row = {"name": "Entry", "elf_address": 0x4000, "file_offset": 0,
        "analysis_address": "0x9000", "resolved_address": "0x9000", "ok": True}
    normalized = {"procedure": {"address": "0x9000", "body": {
        "ranges": [{"start": "0x9000", "end": "0x9007"}]}},
        "native_value_flow": {"available": True, "operations": [{"address": "0x9000"}]},
        "pseudocode": "synthetic fixture",
        "assembly": ["0x9000: synthetic", "0x9004: synthetic"], "callers": [], "callees": [],
        "incoming_references": [], "outgoing_references": [{"source_address": "0x9000",
            "target_address": "0xa000", "kind": {"call": True}}], "unresolved_calls": [],
        "facet_availability": {key: {"available": True} for key in
            ("pseudocode", "assembly", "callers", "callees", "references")}}
    result = {"ok": True, "value": {"subject": {"digest": {"sha256": digest}},
        "provider": {"id": "ghidra"}, "normalized_result": normalized}}
    files = {static / "manifest.json": manifest, static / "dossiers.json": {"Entry": entry},
        exported / "summary.json": {"source_sha256": digest, "rows": [row]},
        exported / "image.json": {"ok": True, "value": {"result": {
            "status": "unsupported", "observations": observations}}},
        exported / "Entry.json": result}
    for path, value in files.items():
        path.write_text(json.dumps(value))
    (static / "entry.asm").write_text("0x4000: synthetic\n0x4004: synthetic\n")
    return static, exported, source, digest


def mutate(path, edit):
    value = json.loads(path.read_text())
    edit(value)
    path.write_text(json.dumps(value))


def test_exact_fixture_with_nonidentity_mapping(dataset):
    report = VERIFIER.verify(*dataset)
    assert report["counts"]["exact_instruction_extents"] == 1
    assert report["load_image_status"] == "unsupported"
    assert report["native_execution"] == "not_run"
    assert report["vm_framework_free"] == "inconclusive"


def test_binary_tamper(dataset):
    dataset[2].write_bytes(b"changed")
    with pytest.raises(ValueError, match="Local binary fingerprint"):
        VERIFIER.verify(*dataset)


@pytest.mark.parametrize("mutation", [
    lambda item: item["value"]["subject"]["digest"].update(sha256="other"),
    lambda item: item["value"]["normalized_result"]["procedure"].update(address="0x8ffc"),
    lambda item: item["value"]["normalized_result"].update(pseudocode=""),
    lambda item: item["value"]["normalized_result"].update(assembly=["0x9004: ret"]),
    lambda item: item["value"]["normalized_result"]["assembly"].reverse(),
    lambda item: item["value"]["normalized_result"]["assembly"].append("0x9004: ret"),
    lambda item: item["value"]["normalized_result"]["outgoing_references"].clear(),
    lambda item: item["value"]["normalized_result"]["facet_availability"]["references"].update(available=False),
    lambda item: item["value"]["normalized_result"].update(callers=None),
    lambda item: item["value"]["normalized_result"]["native_value_flow"]
        ["operations"].append({"address": "0x9010"}),
    lambda item: item["value"]["normalized_result"]["native_value_flow"].update(available=False),
])
def test_reject_false_success(dataset, mutation):
    mutate(dataset[1] / "Entry.json", mutation)
    with pytest.raises(ValueError):
        VERIFIER.verify(*dataset)


def test_failure_must_be_declared_and_retained(dataset):
    mutate(dataset[1] / "summary.json", lambda item: item["rows"][0].update(ok=False))
    (dataset[1] / "Entry.json").write_text(json.dumps({"ok": False, "error": "missing"}))
    with pytest.raises(ValueError, match="Unapproved failed"):
        VERIFIER.verify(*dataset)
    report = VERIFIER.verify(*dataset, allowed_failures=["Entry"])
    assert report["failed_entries"] == ["Entry"]
    assert report["status"] == "verified_with_declared_limits"


def test_boundary_extension_requires_explicit_exception(dataset):
    mutate(dataset[1] / "Entry.json", lambda item: item["value"]["normalized_result"]
        ["assembly"].append("0x9008: synthetic"))
    with pytest.raises(ValueError, match="Unapproved function boundary"):
        VERIFIER.verify(*dataset)
    report = VERIFIER.verify(*dataset, allowed_extensions=["Entry"])
    assert report["extended_bodies"] == [{"name": "Entry", "extra_instructions": 1}]


def test_duplicate_and_missing_rows_rejected(dataset):
    mutate(dataset[1] / "summary.json", lambda item: item["rows"].append(copy.deepcopy(item["rows"][0])))
    with pytest.raises(ValueError, match="Dossier set"):
        VERIFIER.verify(*dataset)


def test_ambiguous_mapping_rejected(dataset):
    mutate(dataset[1] / "image.json", lambda item: item["value"]["result"]["observations"]
        ["mappings"].append(copy.deepcopy(item["value"]["result"]["observations"]["mappings"][0])))
    with pytest.raises(ValueError, match="Non-unique matching"):
        VERIFIER.verify(*dataset)


def test_source_mismatch_rejected(dataset):
    mutate(dataset[1] / "image.json", lambda item: item["value"]["result"]["observations"]
        ["source_files"][0].update(original_sha256="other"))
    with pytest.raises(ValueError, match="matching original"):
        VERIFIER.verify(*dataset)


def test_full_extent_must_be_backed(dataset):
    mutate(dataset[0] / "manifest.json", lambda item: item["segments"][0].update(filesz=4))
    with pytest.raises(ValueError, match="file-backed ELF"):
        VERIFIER.verify(*dataset)


def test_summary_address_must_match(dataset):
    mutate(dataset[1] / "summary.json", lambda item: item["rows"][0].update(analysis_address="0x4000"))
    with pytest.raises(ValueError, match="mapping mismatch"):
        VERIFIER.verify(*dataset)


def test_path_traversal_rejected(dataset):
    mutate(dataset[0] / "dossiers.json", lambda item: item["Entry"].update(assembly="../outside.asm"))
    with pytest.raises(ValueError, match="escapes"):
        VERIFIER.verify(*dataset)


def test_unknown_exception_rejected(dataset):
    with pytest.raises(ValueError, match="Unknown exception"):
        VERIFIER.verify(*dataset, allowed_failures=["Misspelled"])
