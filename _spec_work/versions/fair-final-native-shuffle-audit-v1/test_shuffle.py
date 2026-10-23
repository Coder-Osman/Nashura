import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shuffle_audit import (
    INITIALIZERS, LIBG_SHA, REGIONS, build_report, normal_cleanup_paths, table_target)


def lifetime():
    return {0x100: ("cbz", "w0, #0x110"), 0x104: ("bl", "#0x800"),
        0x108: ("b", "#0x110"), 0x110: ("bl", "#0x900"), 0x114: ("ret", "")}


def cleanup(instructions):
    return normal_cleanup_paths(instructions, 0x100, clear=0x900, generate=0x800)


def test_both_normal_branches_clear_the_same_object():
    report = cleanup(lifetime())
    assert report["normal_returns"] == ["0x114"]
    assert report["clear_calls"] == ["0x110"]


def test_loop_has_finite_audit_and_all_normal_returns_clear():
    instructions = lifetime()
    instructions[0x104] = ("cbnz", "w1, #0x100")
    assert cleanup(instructions)["normal_returns"] == ["0x114"]


@pytest.mark.parametrize("mutation", ["bypass", "tail", "indirect", "exception", "double", "after", "no_return"])
def test_lifetime_rejects_unresolved_or_broken_cleanup(mutation):
    instructions = lifetime()
    if mutation == "bypass":
        instructions[0x100] = ("cbz", "w0, #0x114")
    elif mutation == "tail":
        instructions[0x100] = ("b", "#0x200")
    elif mutation == "indirect":
        instructions[0x104] = ("blr", "x8")
    elif mutation == "exception":
        instructions[0x104] = ("brk", "#0")
    elif mutation == "double":
        instructions[0x114] = ("bl", "#0x900")
    elif mutation == "after":
        instructions[0x114] = ("bl", "#0x800")
    else:
        instructions[0x114] = ("b", "#0x114")
    with pytest.raises(ValueError):
        cleanup(instructions)


def test_dispatch_reads_unsigned_halfword_and_four_byte_offsets():
    binary = SimpleNamespace(get_content_from_virtual_address=lambda address, size: b"\x00\x80")
    assert table_target(binary, table=0x200, index=2, count=3, base=0x100)["target"] == "0x20100"


@pytest.mark.parametrize("index", [-1, 3, True, 1.5])
def test_dispatch_rejects_out_of_range_and_noninteger_index(index):
    binary = SimpleNamespace(get_content_from_virtual_address=lambda address, size: b"\0\0")
    with pytest.raises(ValueError):
        table_target(binary, table=0x200, index=index, count=3, base=0x100)


def test_dispatch_rejects_short_read():
    binary = SimpleNamespace(get_content_from_virtual_address=lambda address, size: b"\0")
    with pytest.raises(ValueError):
        table_target(binary, table=0x200, index=0, count=1, base=0x100)


def test_report_preserves_unresolved_coverage():
    report = json.loads((Path(__file__).parent / "static-report.json").read_text())
    assert report["libg_sha256"] == LIBG_SHA
    assert {name: row["sha256"] for name, row in report["regions"].items()} == {
        name: region[2] for name, region in REGIONS.items()}
    assert len(report["initializer_owners"]) == len(INITIALIZERS) == 18
    assert sum(row["role"] == "unresolved" for row in report["initializer_owners"]) == 16
    assert all(report[name] is False for name in
        ("hidden_random_coverage_closed", "training_admissible", "gates_closed"))


@pytest.fixture(scope="module")
def native_library():
    path = Path(__file__).parent.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not path.exists():
        pytest.skip("private native library omitted from public snapshot")
    return path


def test_all_native_regions_ownership_dispatch_and_lifetime(native_library):
    recorded = json.loads((Path(__file__).parent / "static-report.json").read_text())
    assert build_report(native_library) == recorded


@pytest.mark.parametrize("mutation", ["region", "owner", "census", "factory", "subtype", "relocation"])
def test_reject_native_metadata_and_code_tampering(native_library, monkeypatch, mutation):
    import lief

    original = lief.parse(str(native_library))
    class Altered:
        def get_content_from_virtual_address(self, address, size):
            content = list(original.get_content_from_virtual_address(address, size))
            if ((mutation == "region" and address == REGIONS["hand_shuffle"][0])
                    or (mutation == "factory" and address == 0x341b38 + 1546 * 2)
                    or (mutation == "subtype" and address == 0x34274e + 55 * 2)):
                content[0] ^= 1
            return content
        @property
        def functions(self):
            return [SimpleNamespace(address=entry.address, size=entry.size + int(
                mutation == "owner" and entry.address == 0xf3f0ac)) for entry in original.functions]
        @property
        def segments(self):
            segments = list(original.segments)
            return [] if mutation == "census" else segments
        @property
        def relocations(self):
            return [SimpleNamespace(address=entry.address, type=entry.type,
                addend=entry.addend + int(mutation == "relocation" and entry.address == 0x1877bc8))
                for entry in original.relocations]
    monkeypatch.setattr(lief, "parse", lambda path: Altered())
    with pytest.raises(ValueError):
        build_report(native_library)


def test_reject_wrong_binary_before_disassembly(tmp_path):
    path = tmp_path / "changed.so"
    path.write_bytes(b"changed native binary")
    with pytest.raises(ValueError, match="identity"):
        build_report(path)
