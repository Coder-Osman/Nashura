from contextlib import contextmanager
from pathlib import Path
import struct
import signal

import pytest

from tools import fair_final_prng_audit
from tools.fair_final_prng_audit import NativePrngAudit, SYMBOLS, mapped_base, mapped_guest_pointer, next_native_state, parse_symbols, process_start_ticks


@pytest.mark.parametrize("state", [0, 1, 12345678, 0x80000000, 0xffffffff])
def test_native_prng_state_is_uint32_and_nontrivial(state):
    result = next_native_state(state)
    assert 0 <= result <= 0xffffffff and result != state
    assert next_native_state(state) == result


def test_native_prng_uses_arithmetic_not_logical_shift():
    assert next_native_state(1) == 270369
    assert next_native_state(0) == next_native_state(0xffffffff) == 253983


@pytest.mark.parametrize("state", [-1, 0x100000000, True, 1.0, "1"])
def test_native_prng_rejects_invalid_state(state):
    with pytest.raises(ValueError):
        next_native_state(state)


def test_native_audit_symbols_are_complete_and_unique():
    lines = [f"1: {index:016x} 8 OBJECT LOCAL DEFAULT 25 {name}"
        for index, name in enumerate(SYMBOLS.values(), start=1)]
    assert set(parse_symbols("\n".join(lines))) == set(SYMBOLS)
    for invalid in (lines[:-1], lines + lines[:1], [line.replace("OBJECT", "FUNC") for line in lines]):
        with pytest.raises(ValueError):
            parse_symbols("\n".join(invalid))


def test_native_mapping_requires_exact_path_and_one_zero_offset_mapping():
    mapping = "1000-2000 r--p 00000000 00:01 1 /owned/lib.so\n2000-3000 r-xp 00001000 00:01 1 /owned/lib.so"
    assert mapped_base(mapping, "/owned/lib.so") == 0x1000
    for invalid in (mapping.replace("/owned/", "/other/"), mapping + "\n" + mapping, mapping + " (deleted)"):
        if invalid.endswith(" (deleted)"):
            invalid = mapping.replace("/owned/lib.so", "/owned/lib.so (deleted)")
        with pytest.raises(ValueError):
            mapped_base(invalid, "/owned/lib.so")


def test_start_ticks_handles_spaces_and_parentheses_in_command_name():
    assert process_start_ticks("12 (a name (with) parens) S " + " ".join(map(str, range(1, 25)))) == 19


def test_tagged_guest_pointer_requires_a_unique_complete_writable_mapping():
    maps = "2000-4000 rw-p 00000000 00:00 0"
    assert mapped_guest_pointer(0xb400000000001100, 0x1000, maps, 0x220) == 0x2100
    assert mapped_guest_pointer(0x1100, 0x1000, maps, 0x220) == 0x2100
    for invalid in (maps.replace("rw-p", "r--p"), maps.replace("4000", "2200"),
            maps + "\nb400000000002000-b400000000004000 rw-p 00000000 00:00 0"):
        with pytest.raises(ValueError):
            mapped_guest_pointer(0xb400000000001100, 0x1000, invalid, 0x220)


@pytest.fixture
def memory_audit(tmp_path, monkeypatch):
    audit = NativePrngAudit.__new__(NativePrngAudit)
    audit.probe, audit.libg = Path("/owned/probe.so"), Path("/owned/libg.so")
    audit.pid, audit.record = 123, dict(child_start_ticks="77")
    audit.symbols = dict(manager=8, generation=16, epoch=24, tick=32, worker=40, libg=48)
    (tmp_path / "maps").write_text("1000-2000 rw-p 00000000 00:00 1 /owned/probe.so\n"
        "3000-4000 r-xp 00000000 00:00 2 /owned/libg.so\n5000-6000 rw-p 00000000 00:00 0\n")
    (tmp_path / "mem").touch()
    memory = {0x1008: struct.pack("<Q", 0xb400000000005000), 0x1010: struct.pack("<Q", 3),
        0x1018: struct.pack("<Q", 4), 0x1020: struct.pack("<i", 100), 0x1028: b"\0",
        0x1030: struct.pack("<Q", 0x3000), 0x5060: struct.pack("<i", 100), 0x50a0: struct.pack("<I", 1)}
    writes = []

    @contextmanager
    def paused():
        yield tmp_path

    def write(descriptor, content, address):
        writes.append((address, content))
        memory[address] = content
        return len(content)

    monkeypatch.setattr(audit, "_paused", paused)
    monkeypatch.setattr(fair_final_prng_audit.os, "pread", lambda descriptor, size, address: memory[address][:size])
    monkeypatch.setattr(fair_final_prng_audit.os, "pwrite", write)
    return audit, memory, writes


def test_native_inspection_is_readonly_and_mutation_writes_only_prng(memory_audit):
    audit, memory, writes = memory_audit
    identity = dict(generation=3, stateEpoch=4, tick=100)
    assert audit.inspect(identity)["before"] == 1
    assert writes == []
    result = audit.inspect(identity, advance=True, expected=1)
    assert result["after"] == 270369
    assert writes == [(0x50a0, struct.pack("<I", 270369))]
    assert audit.inspect(identity)["before"] == 270369


@pytest.mark.parametrize("field", ["generation", "stateEpoch", "tick", "expected", "worker", "manager_tick"])
def test_invalid_memory_audit_performs_no_write(memory_audit, field):
    audit, memory, writes = memory_audit
    identity = dict(generation=3, stateEpoch=4, tick=100)
    expected = 1
    if field in identity:
        identity[field] += 1
    elif field == "expected":
        expected = 2
    elif field == "worker":
        memory[0x1028] = b"\1"
    else:
        memory[0x5060] = struct.pack("<i", 101)
    with pytest.raises(ValueError):
        audit.inspect(identity, advance=True, expected=expected)
    assert writes == []
    assert struct.unpack("<I", memory[0x50a0])[0] == 1


@pytest.mark.parametrize("reuse_pid", [False, True])
def test_audit_resumes_owned_process_on_exception_but_not_reused_pid(tmp_path, monkeypatch, reuse_pid):
    audit = NativePrngAudit.__new__(NativePrngAudit)
    audit.pid, audit.record = 123, dict(child_start_ticks="19")
    stat = "123 (owned engine) T " + " ".join(map(str, range(1, 25)))
    (tmp_path / "stat").write_text(stat)
    thread = tmp_path / "task/123"
    thread.mkdir(parents=True)
    (thread / "stat").write_text(stat)
    signals = []
    monkeypatch.setattr(audit, "_verify_process", lambda: tmp_path)
    monkeypatch.setattr(fair_final_prng_audit.os, "kill", lambda pid, value: signals.append((pid, value)))
    with pytest.raises(RuntimeError, match="fixture failure"):
        with audit._paused():
            if reuse_pid:
                (tmp_path / "stat").write_text(stat.replace(" 19 ", " 99 "))
            raise RuntimeError("fixture failure")
    expected = [(123, signal.SIGSTOP)]
    if not reuse_pid:
        expected.append((123, signal.SIGCONT))
    assert signals == expected


def test_audit_signals_nothing_when_process_ownership_check_fails(monkeypatch):
    audit = NativePrngAudit.__new__(NativePrngAudit)
    audit.pid = 123
    signals = []

    def reject():
        raise ValueError("ownership")

    monkeypatch.setattr(audit, "_verify_process", reject)
    monkeypatch.setattr(fair_final_prng_audit.os, "kill", lambda *args: signals.append(args))
    with pytest.raises(ValueError, match="ownership"):
        with audit._paused():
            raise AssertionError("must not enter audit")
    assert not signals
