from pathlib import Path

import pytest

from tools.fair_final_action_random import CLASS_NAME, TARGET_VTABLE, static_layout, walk_actions
from native_runner.tests.test_fair_final_prng_audit import memory_audit


@pytest.fixture
def graph():
    memory = {0x10a8: 0x2000, 0x2008: 0x1000, 0x20e0: 0x3000, 0x3010: 0x4000,
        0x4008: 0x5000, 0x4010: 1, 0x4014: 1, 0x5000: 0x3000, 0x3008: 17,
        0x3028: 0x6000, 0x6050: 0x7000, 0x6058: 1, 0x605c: 1, 0x7000: 0x8000,
        0x8000: 0x10000000 + TARGET_VTABLE, 0x8020: 0x3000,
        0x3078: 1, 0x30ac: 26000000, 0x80c0: 42}

    def read(address, format):
        return memory[address]

    def resolve(pointer, size):
        if pointer == 0 or not 0x1000 <= pointer < 0x9000 or pointer + size > 0x9000:
            raise ValueError("invalid fixture mapping")
        return pointer

    return memory, read, resolve


def test_action_target_is_resolved_by_owner_graph_and_vtable(graph):
    memory, read, resolve = graph
    result = walk_actions(read, resolve, 0x1000, 0x10000000)
    assert result["object_count"] == result["queue_count"] == result["runtime_count"] == 1
    assert result["matches"] == [dict(entity_id=17, owner=1, card_id=26000000, slot=0, state=42)]


def test_other_action_does_not_read_unproven_random_field(graph):
    memory, read, resolve = graph
    memory[0x8000] += 8
    del memory[0x80c0]
    assert walk_actions(read, resolve, 0x1000, 0x10000000)["matches"] == []


@pytest.mark.parametrize("address,value", [(0x2008, 0x1100), (0x4014, 2), (0x605c, -1),
    (0x6058, 257), (0x8020, 0x3100), (0x7000, 0), (0x3008, 0), (0x8000, 8), (0x6050, 0)])
def test_invalid_graph_rejected_before_claiming_absence(graph, address, value):
    memory, read, resolve = graph
    memory[address] = value
    with pytest.raises(ValueError):
        walk_actions(read, resolve, 0x1000, 0x10000000)


def test_empty_action_vector_is_valid_without_dereferencing_pointer(graph):
    memory, read, resolve = graph
    memory.update({0x6050: 0, 0x6058: 0, 0x605c: 0})
    result = walk_actions(read, resolve, 0x1000, 0x10000000)
    assert result["queue_count"] == 1 and result["runtime_count"] == 0 and not result["matches"]


def test_target_type_static_evidence_is_bound_to_frozen_library(tmp_path):
    source = Path(__file__).resolve().parents[2] / "artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not source.exists():
        pytest.skip("fixed native artifact not installed")
    result = static_layout(source)
    assert result["reflected_class"] == CLASS_NAME
    assert result["runtime_vtable"] == hex(TARGET_VTABLE)
    assert result["dispatch"]["0x189f9e8"] == "0xf4f9b4"
    invalid = tmp_path / "libg.so"
    invalid.write_bytes(b"invalid")
    with pytest.raises(ValueError, match="identity"):
        static_layout(invalid)


@pytest.mark.parametrize("field", ["generation", "stateEpoch", "tick", "worker", "manager_tick"])
def test_action_audit_rejects_stale_identity_before_traversal(memory_audit, monkeypatch, field):
    import struct
    from tools import fair_final_action_random

    audit, memory, writes = memory_audit
    audit.__class__ = fair_final_action_random.NativeActionRandomAudit
    identity = dict(generation=3, stateEpoch=4, tick=100)
    if field in identity:
        identity[field] += 1
    elif field == "worker":
        memory[0x1028] = b"\1"
    else:
        memory[0x5060] = struct.pack("<i", 101)
    traversals = []
    monkeypatch.setattr(fair_final_action_random, "walk_actions", lambda *args: traversals.append(True))
    with pytest.raises(ValueError):
        audit.inspect(identity)
    assert not traversals and not writes


def test_action_audit_uses_readonly_descriptor(memory_audit, monkeypatch):
    from tools import fair_final_action_random

    audit, memory, writes = memory_audit
    audit.__class__ = fair_final_action_random.NativeActionRandomAudit
    original_open = fair_final_action_random.os.open
    modes = []

    def record_open(path, mode):
        modes.append(mode)
        return original_open(path, mode)

    monkeypatch.setattr(fair_final_action_random.os, "open", record_open)
    monkeypatch.setattr(fair_final_action_random, "walk_actions", lambda *args: dict(matches=[]))
    result = audit.inspect(dict(generation=3, stateEpoch=4, tick=100))
    assert modes == [fair_final_action_random.os.O_RDONLY] and result["readonly"] and not writes
