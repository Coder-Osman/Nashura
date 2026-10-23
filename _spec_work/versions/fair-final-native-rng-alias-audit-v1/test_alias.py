import copy
import json
from pathlib import Path

import pytest

from alias_audit import TIME_PATH, build_report, tail_reference_closure, validate_time_path


def test_transitive_tail_entries_include_calls_to_entry_but_not_calls_to_bl_site():
    edges = [(0x100, 0x900, "b"), (0x200, 0x100, "b"), (0x300, 0x200, "bl"),
        (0x400, 0x300, "bl"), (0x500, 0x900, "bl")]
    assert tail_reference_closure(edges, 0x900) == [
        dict(location="0x100", target="0x900", opcode="b"),
        dict(location="0x200", target="0x100", opcode="b"),
        dict(location="0x300", target="0x200", opcode="bl"),
        dict(location="0x500", target="0x900", opcode="bl")]


def test_tail_cycle_terminates_and_sorting_is_stable():
    edges = [(0x100, 0x200, "b"), (0x200, 0x100, "b")]
    assert tail_reference_closure(edges, 0x100) == tail_reference_closure(reversed(edges), 0x100)
    assert len(tail_reference_closure(edges, 0x100)) == 2


def test_unreferenced_target_has_empty_closure():
    assert tail_reference_closure([(0x100, 0x200, "bl")], 0x900) == []


def test_out_of_mapping_signed_branch_target_does_not_discard_valid_edges():
    assert tail_reference_closure([(0x100, -4, "b"), (0x200, 0x900, "bl")], 0x900) == [
        dict(location="0x200", target="0x900", opcode="bl")]


@pytest.mark.parametrize("target", [True, -4, 1, 4.0])
def test_invalid_root_target(target):
    with pytest.raises(ValueError):
        tail_reference_closure([], target)


@pytest.mark.parametrize("edges", [
    [(0x100, 0x900, "cbz")], [(0x101, 0x900, "b")], [(0x100, 0x901, "b")],
    [(True, 0x900, "b")], [(-4, 0x900, "b")],
    [(0x100, 0x900, "b"), (0x100, 0x900, "b")]])
def test_invalid_or_overlapping_census_fails_closed(edges):
    with pytest.raises(ValueError):
        tail_reference_closure(edges, 0x900)


@pytest.mark.parametrize("index", range(len(TIME_PATH)))
def test_seed_path_contract_rejects_each_mutated_instruction(index):
    instructions = list(TIME_PATH)
    instructions[index] = ("nop", "")
    with pytest.raises(ValueError):
        validate_time_path(instructions)


def test_identity_is_checked_before_native_parsing(tmp_path):
    library = tmp_path / "wrong-library"
    library.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="library identity"):
        build_report(library)


def test_recorded_evidence_keeps_actual_coverage_open():
    report = json.loads((Path(__file__).parent / "static-report.json").read_text())
    assert (report["direct_count"], report["closure_count"]) == (18, 19)
    assert report["additional_references"] == [
        dict(location="0x1a1cb30", target="0x7f5434", opcode="b")]
    assert all(report[key] is False for key in
        ("hidden_random_coverage_closed", "training_admissible", "gates_closed"))
    assert all(report["time_seed"][key] is False for key in
        ("path_entry_reachable_proven", "fixed_hog_usage_proven", "snapshot_coverage_proven"))


@pytest.fixture(scope="module")
def native_library():
    path = Path(__file__).parent.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not path.exists():
        pytest.skip("private native library omitted from public snapshot")
    return path


def test_actual_library_recomputes_recorded_report(native_library):
    recorded = json.loads((Path(__file__).parent / "static-report.json").read_text())
    assert json.loads(json.dumps(build_report(native_library))) == recorded


@pytest.mark.parametrize("mutation", ["path", "entry", "direct", "closure", "time_import"])
def test_native_audit_rejects_contract_tampering(native_library, monkeypatch, mutation):
    import alias_audit

    if mutation in ("path", "entry"):
        regions = copy.deepcopy(alias_audit.REGIONS)
        key = "time_seed_path" if mutation == "path" else "tail_entry"
        address, size, digest = regions[key]
        regions[key] = (address, size, "0" * 64)
        monkeypatch.setattr(alias_audit, "REGIONS", regions)
    elif mutation == "direct":
        monkeypatch.setattr(alias_audit, "EXPECTED_DIRECT", alias_audit.EXPECTED_DIRECT - {0x7f5434})
    elif mutation == "closure":
        monkeypatch.setattr(alias_audit, "tail_reference_closure", lambda edges, target: [])
    else:
        import lief

        original = lief.parse(str(native_library))
        class Altered:
            def __getattr__(self, name):
                if name == "relocations":
                    return [entry for entry in original.relocations if entry.address != 0x18e96f0]
                return getattr(original, name)
        monkeypatch.setattr(lief, "parse", lambda path: Altered())
    with pytest.raises(ValueError):
        build_report(native_library)
