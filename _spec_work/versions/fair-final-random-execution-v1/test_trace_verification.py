from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zlib

import pytest

from trace_protocol import LOG_FLAGS, filter_spec, sha, summarize
from verify_trace import address_identity, audit_execution_attempts, limited, validate_native, validate_trajectory


def choice():
    content = bytes([0, 1, *([0] * 29)])
    return dict(action=dict(card_id=26000021, cell=0, delay_slot=1), fair_sha="a" * 64,
        mask=dict(card_ids=[26000021], shape=[1, 1, 31], modes=[True, True],
            plays_zlib_hex=zlib.compress(content).hex(), plays_sha256=hashlib.sha256(content).hexdigest()))


def trajectory():
    wait = choice()
    wait["action"] = dict(card_id=None, cell=None, delay_slot=None)
    return dict(opening=dict(tick=90), final=dict(tick=93), steps=3, executed=2, policy_closed_tick=93,
        final_clock=dict(schema="fair-final-decision-clock.v1", tick=93, next_identity=4, pending=[], ready=[],
            evidence=[dict(reservation=owner, owner=owner, tick=92, reason="executed") for owner in (0, 1)]
                + [dict(reservation=owner+2, owner=owner, tick=93, reason="terminal_expired") for owner in (0, 1)]),
        terminal=dict(finalized=True, worldResult=0, worldResultRaw=0, winner=0, tick=93, crownsRaw=[1, 0]),
        decisions=[dict(tick=90, choices={"0": choice(), "1": choice()}),
            dict(tick=92, choices={"0": deepcopy(wait), "1": deepcopy(wait)})],
        outcomes=[dict(tick=92, outcomes={"0": "executed", "1": "executed"})])


def test_complete_simultaneous_legal_trajectory_and_terminal():
    result = validate_trajectory(trajectory())
    assert result == dict(decision_groups=2, decisions=4, executed_by_owner={0: 1, 1: 1}, final_tick=93, winner=0)


@pytest.mark.parametrize("change", ["truncated", "label", "tick", "missing_owner", "early_decision",
    "wrong_outcome", "missing_outcome", "unvisited_tick", "duplicate", "illegal", "executed"])
def test_trajectory_damage_rejected(change):
    value = trajectory()
    if change == "truncated": value["terminal"]["finalized"] = False
    elif change == "label": value["terminal"]["winner"] = 1
    elif change == "tick": value["steps"] = 4
    elif change == "missing_owner": del value["decisions"][0]["choices"]["1"]
    elif change == "early_decision": value["decisions"][1]["tick"] = 91
    elif change == "wrong_outcome": value["outcomes"][0]["outcomes"]["0"] = "rejected"
    elif change == "missing_outcome": value["outcomes"] = []
    elif change == "unvisited_tick": value["outcomes"][0]["tick"] = 95
    elif change == "duplicate": value["decisions"].append(deepcopy(value["decisions"][0]))
    elif change == "illegal": value["decisions"][0]["choices"]["0"]["action"]["delay_slot"] = 2
    else: value["executed"] = 1
    with pytest.raises(ValueError):
        validate_trajectory(value)


def test_real_draw_requires_equal_crowns():
    value = trajectory()
    value["terminal"].update(worldResult=-1, worldResultRaw=-1, winner=None, crownsRaw=[1, 1])
    assert validate_trajectory(value)["winner"] is None
    value["terminal"]["crownsRaw"] = [0, 1]
    with pytest.raises(ValueError, match="draw"):
        validate_trajectory(value)


def test_address_identity_and_diagnostic_scope():
    process = dict(child_pid=123, child_start_ticks="456")
    state = dict(readonly=True, **process, libg_base=4096, manager=8192, tick=90, generation=1, stateEpoch=2)
    address_identity(state, process)
    state["child_start_ticks"] = "457"
    with pytest.raises(ValueError, match="identity"):
        address_identity(state, process)
    state.update(child_start_ticks="456", manager=0, tick=-1, generation=0, stateEpoch=0)
    address_identity(state, process, cold=True)
    with pytest.raises(ValueError, match="identity"):
        address_identity(state, process)
    limited(dict(training_admissible=False, gates_closed=False))
    with pytest.raises(ValueError, match="scope"):
        limited(dict(training_admissible=True, gates_closed=False))


def test_actual_failed_archive_is_rejected():
    from verify_trace import verify

    root = Path(__file__).resolve().parent
    attempt = root / "native-execution-1792743535284451000"
    if not attempt.exists():
        pytest.skip("private failed native trace is excluded from public snapshot")
    parent = root.parent / "fair-final-global-rng-snapshot-v1/native-snapshot-1792734107610915000"
    with pytest.raises(ValueError, match="missing evidence or archived failure"):
        verify(attempt, sha(attempt / "launch.json"), sha(attempt / "download.json"), parent=parent)


@pytest.fixture
def native_archive(tmp_path):
    from test_trace import BASE, dump

    folder = tmp_path / "trace"
    (folder / "engine").mkdir(parents=True)
    settings = dict(qemu="/qemu", qemu_sha256="a" * 64, service_sha256="b" * 64,
        startup_service_sha256="c" * 64, engine_sha256="d" * 64, root="/remote", port=29785,
        probe="/engine/libcrprobe.so", source_sha256={"trace_launcher.py": "e" * 64,
            "trace_protocol.py": "f" * 64})
    configuration = dict(mode="trace", center_base=BASE, qemu_sha256=settings["qemu_sha256"],
        service_sha256=settings["service_sha256"])
    write = lambda name, value: (folder / name).write_text(json.dumps(value))
    write("trace-settings.json", configuration)
    attestation = dict(settings_sha256=sha(folder / "trace-settings.json"),
        launcher_sha256="e" * 64, protocol_sha256="f" * 64, startup_deadline_seconds=600,
        startup_service_sha256="c" * 64)
    process = dict(pid=120, start_ticks="456", child_pid=123, child_start_ticks="789",
        phase="stopped", ready=False, port=29785, random_execution_trace=attestation,
        phase_snapshot_engine=dict(path=settings["probe"], sha256=settings["engine_sha256"],
            launcher_sha256="e" * 64, service_sha256="b" * 64),
        argv=["/qemu", "-d", LOG_FLAGS, "-dfilter", filter_spec(BASE)["expression"],
            "-D", "/remote/trace/qemu.log", settings["probe"], "29785"])
    identity = dict(pid=120, start_ticks="456")
    write("engine/process.json", process)
    write("engine-owner.json", dict(process=identity))
    write("cleanup.json", dict(clean=True, port_released=True, exit_code=0, process=process, launcher=identity))
    write("probe-process.json", dict(exit_code=0, seconds=4))
    address = dict(readonly=True, child_pid=123, child_start_ticks="789", libg_base=BASE,
        manager=8192, tick=90, generation=1, stateEpoch=1)
    before = dict(address, manager=0, tick=-1, generation=0, stateEpoch=0)
    write("address-evidence.json", before)
    reset = dump(0x121B2E4, object=8192+0xA0) + dump(0x121B118, object=BASE+0x19FC368)
    content = reset + dump(0x121B350, object=8192+0xA0)
    (folder / "qemu.log").write_bytes(content)
    report = dict(trajectory(), schema="FairFinalRandomExecutionProbe/v1", mode="trace",
        status="completed_pending_trace_verification", engine_sha256=settings["engine_sha256"],
        source_sha256={"native.py": "1" * 64}, match_seed=78102011, execution_seed=78102021,
        seconds=3, training_admissible=False, gates_closed=False, before=before,
        reset_offset=0, battle_offset=len(reset), final_offset=len(content))
    report.update(opening=address, final=dict(address, tick=93))
    write("report.json", report)
    summary = summarize(folder / "qemu.log", base=BASE, reset_offset=0, battle_offset=len(reset),
        final_offset=len(content), global_object=BASE+0x19FC368, manager_object=8192+0xA0)
    write("trace-summary.json", summary)
    return folder, settings, BASE


def test_native_archive_recomputes_raw_register_evidence(native_archive):
    folder, settings, base = native_archive
    _, _, result = validate_native(folder, "trace", settings, {"native.py": "1" * 64}, center=base)
    assert result["captured_entries"] == 3
    assert result["phase_counts"]["battle"] == {"xorshift_generate": 1}
    assert result["trajectory"]["executed_by_owner"] == {0: 1, 1: 1}


@pytest.mark.parametrize("damage", ["raw_log", "summary", "base", "flags", "cleanup", "owner", "truncated",
    "policy_close", "final_clock", "registered_base"])
def test_native_archive_damage_rejected_even_when_json_is_rewritten(native_archive, damage):
    folder, settings, base = native_archive
    if damage == "raw_log":
        (folder / "qemu.log").write_bytes(b"")
    else:
        target = {"summary": "trace-summary.json", "base": "report.json", "flags": "engine/process.json",
            "cleanup": "cleanup.json", "owner": "engine-owner.json", "truncated": "report.json",
            "policy_close": "report.json", "final_clock": "report.json",
            "registered_base": "address-evidence.json"}[damage]
        path = folder / target
        value = json.loads(path.read_text())
        if damage == "summary": value["captured_entries"] = 1000
        elif damage == "base":
            for name in ("before", "opening", "final"): value[name]["libg_base"] += 4096
        elif damage == "flags": value["argv"][2] = "exec,cpu"
        elif damage == "cleanup": value["clean"] = False
        elif damage == "owner": value["process"]["start_ticks"] = "unrelated"
        elif damage == "truncated": value["terminal"]["finalized"] = False
        elif damage == "policy_close": value["policy_closed_tick"] = 91
        elif damage == "final_clock": value["final_clock"]["evidence"] = []
        else: value["libg_base"] += 4096
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        validate_native(folder, "trace", settings, {"native.py": "1" * 64}, center=base)


@pytest.mark.parametrize("damage", ["stopped", "header_pc", "interleaved", "empty"])
def test_qemu_unexecuted_or_ambiguous_attempts_rejected(native_archive, damage):
    folder, _, _ = native_archive
    path = folder / "qemu.log"
    original = path.read_bytes()
    assert audit_execution_attempts(path) == 3
    if damage == "stopped": content = original + b"Stopped execution of TB chain before 0xaaa [0000]\n"
    elif damage == "header_pc": content = original.replace(b" PC=", b" PC=ffffffffffffffff ", 1)
    elif damage == "interleaved": content = original.splitlines(keepends=True)[0] + original
    else: content = b""
    path.write_bytes(content)
    with pytest.raises(ValueError):
        audit_execution_attempts(path)
