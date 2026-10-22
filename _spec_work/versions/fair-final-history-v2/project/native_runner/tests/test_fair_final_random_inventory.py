from pathlib import Path
import struct

import pytest

from tools.fair_final_random_inventory import ANCHORS, build_inventory, direct_branches
from tools.fair_final_random_census import NativeRandomCensus, semantic_census
from native_runner.tests.test_fair_final_prng_audit import memory_audit


def test_direct_branch_decodes_signed_offsets_and_ignores_indirect_calls():
    content = struct.pack("<IIII", 0x94000002, 0x17ffffff, 0xd63f0100, 0xd503201f)
    assert list(direct_branches(content, 0x1000)) == [(0x1000, 0x1008, "bl"), (0x1004, 0x1000, "b")]


@pytest.mark.parametrize("address,content", [(1, b"\0" * 4), (0, b"\0" * 3)])
def test_direct_branch_rejects_unaligned_ranges(address, content):
    with pytest.raises(ValueError, match="unaligned"):
        list(direct_branches(content, address))


def test_inventory_rejects_wrong_binary_before_decoding(tmp_path):
    library = tmp_path / "libg.so"
    library.write_bytes(b"not the frozen engine")
    with pytest.raises(ValueError, match="identity"):
        build_inventory(library)


def test_frozen_native_inventory_anchors_and_call_counts():
    source = Path(__file__).resolve().parents[2] / "artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not source.exists():
        pytest.skip("fixed native artifact not installed")
    report = build_inventory(source)
    assert set(report["anchors"]) == set(ANCHORS)
    assert len(report["calls"]["xorshift_save"]) == 8
    assert len(report["calls"]["xorshift_restore"]) == 8
    assert len(report["calls"]["mt_like_initialize"]) == 18
    assert len(report["calls"]["mt_like_generate"]) == 43
    assert report["training_admissible"] is False
    assert report["unresolved"]


def census_fixture(memory_audit):
    audit, memory, writes = memory_audit
    audit.__class__ = NativeRandomCensus
    memory.update({0x50a8: struct.pack("<Q", 0xb400000000005800),
        0x5808: struct.pack("<Q", 0xb400000000005000),
        0x5810: struct.pack("<Q", 0x5900), 0x5050: bytes(8), 0x5218: bytes(8)})
    return audit, memory, writes


def test_census_reads_branch_presence_without_writing(memory_audit):
    audit, memory, writes = census_fixture(memory_audit)
    result = audit.inspect(dict(generation=3, stateEpoch=4, tick=100))
    assert result["readonly"] and result["world_backlink_matches"]
    assert not result["alternate_50_present"] and not result["alternate_218_present"]
    assert result["manager_state"] == 1
    assert not writes


@pytest.mark.parametrize("field", ["generation", "stateEpoch", "tick", "worker", "world", "backlink", "short_read"])
def test_census_rejects_stale_or_invalid_object_graph(memory_audit, field):
    audit, memory, writes = census_fixture(memory_audit)
    identity = dict(generation=3, stateEpoch=4, tick=100)
    if field in identity:
        identity[field] += 1
    elif field == "worker":
        memory[0x1028] = b"\1"
    elif field == "world":
        memory[0x50a8] = bytes(8)
    elif field == "backlink":
        memory[0x5808] = struct.pack("<Q", 0x5100)
    else:
        memory[0x50a0] = b"\1"
    with pytest.raises(ValueError):
        audit.inspect(identity)
    assert not writes


def test_census_reports_present_alternate_state_instead_of_assuming_absence(memory_audit):
    audit, memory, writes = census_fixture(memory_audit)
    memory[0x5050] = struct.pack("<Q", 0x5800)
    for offset, state in ((0x550, 11), (0x554, 22), (0x558, 33)):
        memory[0x5800 + offset] = struct.pack("<I", state)
    result = audit.inspect(dict(generation=3, stateEpoch=4, tick=100))
    assert result["alternate_50_present"]
    assert result["alternate_50_states"] == {"0x550": 11, "0x554": 22, "0x558": 33}
    assert not writes


def test_semantic_comparison_ignores_only_snapshot_generation_and_epoch():
    original = dict(generation=1, stateEpoch=2, tick=100, manager_state=3, child_pid=4)
    restored = {**original, "generation": 2, "stateEpoch": 5}
    assert semantic_census(original) == semantic_census(restored)
    for key in ("tick", "manager_state", "child_pid"):
        assert semantic_census(original) != semantic_census({**restored, key: restored[key] + 1})


def test_census_opens_process_memory_readonly(memory_audit, monkeypatch):
    from tools import fair_final_random_census

    audit, memory, writes = census_fixture(memory_audit)
    original_open = fair_final_random_census.os.open
    modes = []

    def record_open(path, flags):
        modes.append(flags)
        return original_open(path, flags)

    monkeypatch.setattr(fair_final_random_census.os, "open", record_open)
    audit.inspect(dict(generation=3, stateEpoch=4, tick=100))
    assert modes == [fair_final_random_census.os.O_RDONLY]
    assert not writes
