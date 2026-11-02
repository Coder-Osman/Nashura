"""Current atomic state plus unseen ring events; no reconstructed old payloads."""
from copy import deepcopy
import struct
import pytest

from native_runner.cr_native_env import JsonEventCursor, JSON_EVENT_RINGS, RunnerError
from native_runner.tests import test_native_runtime_telemetry_api as fixtures


def full_capture():
    capture = fixtures._atomic_response()
    rich = capture["rich"]
    for populate in (fixtures._add_damage_combat_event, fixtures._add_phase_runtime,
                     fixtures._add_visibility_runtime, fixtures._add_remaining_runtime,
                     fixtures._add_special_movement_runtime, fixtures._add_action_movement_runtime,
                     fixtures._add_character_state_runtime):
        populate(rich)
    return capture


def delta_after(capture):
    result = deepcopy(capture)
    for name in JSON_EVENT_RINGS:
        ring = result["rich"][name]
        ring["transmitFromSequence"] = ring["nextSequence"]
        ring["events"] = []
    return result


def test_full_then_empty_delta_retains_state_and_only_minimal_cause_summary():
    capture = full_capture()
    cursor = JsonEventCursor()
    assert cursor.validate(capture) == capture
    assert len(cursor.combat) == 1
    assert cursor.combat[1][:3] == ("damage", 500, None)
    delta = delta_after(capture)
    assert cursor.validate(delta) == delta
    assert delta["rich"]["objects"] == capture["rich"]["objects"]
    assert cursor.wire() == (capture["generation"], capture["stateEpoch"],
                             *(capture["rich"][n]["nextSequence"] for n in JSON_EVENT_RINGS))


@pytest.mark.parametrize("name", JSON_EVENT_RINGS)
def test_skipped_ring_rejected_without_advancing_any_cursor(name):
    capture = full_capture()
    cursor = JsonEventCursor(); cursor.validate(capture)
    before = deepcopy(cursor.__dict__)
    delta = delta_after(capture)
    delta["rich"][name]["nextSequence"] += 1
    delta["rich"][name]["transmitFromSequence"] += 1
    with pytest.raises(RunnerError, match="confirmed cursor"):
        cursor.validate(delta)
    assert cursor.__dict__ == before


def test_tick_rollback_requires_restored_cursor_and_epoch_change_reboots():
    capture = full_capture(); cursor = JsonEventCursor(); cursor.validate(capture)
    saved = deepcopy(cursor)
    delta = delta_after(capture)
    for value in (delta, delta["ordinary"], delta["rich"]): value["tick"] += 1
    for name in JSON_EVENT_RINGS: delta["rich"][name]["observationTick"] += 1
    cursor.validate(delta)
    with pytest.raises(RunnerError, match="regressed"):
        cursor.validate(delta_after(capture))
    assert saved.validate(delta_after(capture))
    cursor.clear()
    assert cursor.validate(capture)


def test_new_invalid_record_does_not_commit_cursors_or_summaries():
    capture = full_capture(); cursor = JsonEventCursor(); cursor.validate(capture)
    before = deepcopy(cursor.__dict__); delta = delta_after(capture)
    ring = delta["rich"]["combatEvents"]
    event = deepcopy(capture["rich"]["combatEvents"]["events"][0])
    event.update(sequence=2, actualAmount=-1)
    ring["nextSequence"] = 3; ring["events"] = [event]
    with pytest.raises(RunnerError): cursor.validate(delta)
    assert cursor.__dict__ == before


def test_cross_batch_cause_is_checked_without_old_event_payload():
    capture = full_capture(); cursor = JsonEventCursor(); cursor.validate(capture)
    delta = delta_after(capture); ring = delta["rich"]["combatEvents"]
    event = deepcopy(capture["rich"]["combatEvents"]["events"][0])
    event.update(sequence=2, kind="death", hookOffset=0xF642C4,
                 causeSequence=1, pool="hitpoints", terminalReason="none", lethal=True,
                 preHp=100, postHp=0)
    ring["events"] = [event]; ring["nextSequence"] = 3
    cursor.validate(delta)
    assert cursor.combat[2][0] == "death"
    broken = JsonEventCursor(); broken.validate(capture)
    broken.combat[1] = ("heal", 500, None, None)
    with pytest.raises(RunnerError, match="cause kind"): broken.validate(delta)


def test_native_cursor_bounds_and_epoch_reset(tmp_path):
    import subprocess
    from pathlib import Path
    source = tmp_path / "cursor.cpp"
    header = Path(__file__).resolve().parents[1] / "probe" / "observation_delta.h"
    source.write_text(f'#include "{header}"\n#include <cassert>\n' +
        'int main() { ObservationEventCursor c; assert(parse_observation_cursor("1 2 3 4 5 6 7 8 9", &c)); '
        'assert(c.floor(1,2,0)==3); assert(c.floor(1,3,0)==0); unsigned long long unused=0; (void)unused; '
        'std::uint64_t start=0; assert(observation_event_start(0,3,9,&start)&&start==3); '
        'assert(!observation_event_start(2,3,9,&start)); assert(!observation_event_start(10,3,9,&start)); '
        'assert(!parse_observation_cursor("-1 2 3 4 5 6 7 8 9", &c)); '
        'assert(!parse_observation_cursor("18446744073709551616 2 3 4 5 6 7 8 9", &c)); '
        'assert(!parse_observation_cursor("1 2 3 4 5 6 7 8 9 10", &c)); '
        'assert(parse_observation_cursor("18446744073709551615 2 3 4 5 6 7 8 9", &c)); }')
    binary = tmp_path / "cursor"
    subprocess.run(["clang++", "-std=c++17", str(source), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)


def test_json_batch_cursor_survives_reconnect_and_slots_stay_independent(monkeypatch):
    import orjson
    from native_runner import resident_batch_channel as batch
    from native_runner.tests.test_resident_batch_channel import _ScriptedSocket
    first = full_capture()
    def connection(capture):
        payload = orjson.dumps(capture)
        response = batch._HANDSHAKE.pack(b"CRBH", 1, 0, 16, 0)
        response += batch._HEADER.pack(b"CRBS", 1, 0, 1, 1)
        response += batch._RESPONSE_ENTRY.pack(3, 0, 0, 8, len(payload)) + payload
        return _ScriptedSocket(response)
    sockets = [connection(first), connection(delta_after(first))]
    monkeypatch.setattr(batch.socket, "create_connection", lambda *_a, **_k: sockets.pop(0))
    cursors = {8: JsonEventCursor()}
    for number in range(2):
        channel = batch.ResidentBatchChannelV1(json_event_deltas=True, json_cursors=cursors)
        sock = channel._socket
        outcome = channel.step_partitioned({3: ()}, ticks=1)[3]
        assert isinstance(outcome, batch.BatchStepResultV1)
        handshake = b"batch-json-delta-v1\n"
        assert sock.sent.startswith(handshake)
        offset = len(handshake) + batch._HEADER.size + batch._REQUEST_ENTRY.size
        wire = batch._JSON_CURSOR.unpack(sock.sent[offset:offset+batch._JSON_CURSOR.size])
        assert wire == ((0,) * 9 if number == 0 else cursors[3].wire())
        channel.close()
    assert cursors[8].wire() == (0,) * 9
    assert len(cursors[3].combat) == 1


def test_native_restore_clears_delta_delivery_cursor_only_after_success(monkeypatch):
    from native_runner.cr_native_env import NativeClashEnv
    native = NativeClashEnv(); native.json_event_deltas = True
    native._json_event_cursor.validate(full_capture())
    # Exercise the request result boundary without a real socket.
    from native_runner.tests.test_resident_batch_channel import _ScriptedSocket
    class Socket(_ScriptedSocket):
        def __enter__(self): return self
        def __exit__(self, *_): self.close()
    from native_runner import cr_native_env as module
    responses = [b'{"ok":false,"error":"restore rejected"}', b'{"ok":true}']
    monkeypatch.setattr(module.socket, "create_connection", lambda *_a, **_k: Socket(responses.pop(0)))
    with pytest.raises(RunnerError): native._request("restore 1")
    assert native._json_event_cursor.identity is not None
    native._request("restore 1")
    assert native._json_event_cursor.wire() == (0,) * 9


def test_event_batch_failed_restore_preserves_confirmed_delivery_cursor(monkeypatch):
    from native_runner.training.sspi.collector import EventBatchedSSPINative
    native = EventBatchedSSPINative("127.0.0.1", 1, env_id=3, event_deltas=True)
    native._json_cursors[3].validate(full_capture())
    before = deepcopy(native._json_cursors[3].__dict__)
    monkeypatch.setattr(native, "resident_status",
        lambda: {"occupied": 1, "slots": [{"envId": 3}], "residentSnapshotRestore": True})
    def rejected(*_a, **_k):
        raise RunnerError("restore rejected")
    monkeypatch.setattr(native, "_raw_request", rejected)
    with pytest.raises(RunnerError, match="restore rejected"):
        native._request("restore 1")
    assert native._json_cursors[3].__dict__ == before
    monkeypatch.setattr(native, "_raw_request", lambda *_a, **_k: {"ok": True})
    native._request("snapshot-create")
    assert native._json_cursors[3].__dict__ == before
    native._request("restore 1")
    assert native._json_cursors[3].wire() == (0,) * 9


def test_event_batch_restore_rebases_tick_and_delta_cursor(monkeypatch):
    from native_runner.cr_native_env import NativeClashEnv
    from native_runner.training.sspi.collector import EventBatchedSSPINative

    native = EventBatchedSSPINative("127.0.0.1", 1, env_id=3, event_deltas=True,
        persistent_control=True)
    native.start_atomic_steps(initial_tick=7)
    native._last_tick = 90
    native.last_advance_boundary = object()
    native._json_cursors[3].validate(full_capture())
    monkeypatch.setattr(native, "resident_status",
        lambda: {"occupied": 1, "slots": [{"envId": 3}], "residentSnapshotRestore": True})

    def request(_self, command, *, _read_retry_count=2):
        if command == "env 3 restore 1":
            return {"ok": True, "generation": 2, "stateEpoch": 4, "tick": 42}
        if command == "env 3 observe":
            return {"ok": True, "tick": 42, "players": []}
        raise AssertionError(f"unexpected native command {command!r}")

    monkeypatch.setattr(NativeClashEnv, "_request", request)
    observation = native.restore(1)

    assert observation["tick"] == 42
    assert native._last_tick == 42
    assert native.last_advance_boundary is None
    assert native._json_cursors[3].wire() == (0,) * 9
    assert native._persistent_transport_requested


@pytest.mark.parametrize("public_consumer,expected_floor", [(False, 2), (True, 1)])
def test_only_active_combat_consumers_constrain_delta_merge(monkeypatch, public_consumer, expected_floor):
    from types import SimpleNamespace
    from native_runner import battle_env as module
    capture = delta_after(full_capture())
    env = object.__new__(module.BattleEnvV1)
    env.native = SimpleNamespace(observe_atomic=lambda: capture,
        public_card_play_events_from_combat_ring=public_consumer)
    identity = (capture["generation"], capture["stateEpoch"])
    env._combat_event_identity = env._public_card_play_event_identity = identity
    env._combat_next_sequence = 2
    env._public_card_play_next_sequence = 1
    for ring in ("phase", "visibility", "remaining"):
        setattr(env, f"_{ring}_event_identity", None)
    received = {}
    def bind(raw, rich, **kwargs):
        received.update(kwargs)
        return object()
    monkeypatch.setattr(module, "bind_rich_telemetry", bind)
    env._atomic_observation_pair()
    assert received["combat_event_floor"] == expected_floor


@pytest.mark.parametrize("changed_descriptor", [False, True])
def test_cross_batch_card_play_duplicate_and_descriptor_changes_are_rejected(changed_descriptor):
    capture = full_capture()
    fixtures._add_card_play_combat_event(capture["rich"])
    cursor = JsonEventCursor(); cursor.validate(capture)
    before = deepcopy(cursor.__dict__)
    delta = delta_after(capture)
    event = deepcopy(capture["rich"]["combatEvents"]["events"][0])
    event["sequence"] = 2
    if changed_descriptor:
        event["deploymentContext"]["owner"] = 1 - event["deploymentContext"]["owner"]
    delta["rich"]["combatEvents"].update(events=[event], nextSequence=3)
    with pytest.raises(RunnerError, match="descriptor identity|repeats a card_play"):
        cursor.validate(delta)
    assert cursor.__dict__ == before


def test_identity_change_restarts_summary_and_new_consumer_has_independent_cursor():
    capture = full_capture()
    fixtures._add_card_play_combat_event(capture["rich"])
    first = JsonEventCursor(); first.validate(capture)
    second = JsonEventCursor()
    assert second.wire() == (0,) * 9
    assert second.validate(capture) == capture
    # Equal sequence/deployment IDs in a new identity are not old duplicates.
    changed = deepcopy(capture)
    def rewrite(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "stateEpoch": value[key] += 1
                else: rewrite(item)
        elif isinstance(value, list):
            for item in value: rewrite(item)
    rewrite(changed)
    first.validate(changed)
    assert first.identity != second.identity
    assert len(first.combat) == len(second.combat) == 1


def test_missed_retained_window_is_rejected_without_cursor_commit():
    capture = full_capture(); cursor = JsonEventCursor(); cursor.validate(capture)
    before = deepcopy(cursor.__dict__)
    delta = delta_after(capture)
    delta["rich"]["combatEvents"].update(
        oldestRetainedSequence=3, nextSequence=3, transmitFromSequence=3, events=[])
    with pytest.raises(RunnerError, match="confirmed cursor"):
        cursor.validate(delta)
    assert cursor.__dict__ == before


def test_delta_rejects_validation_bypass(monkeypatch):
    from native_runner import cr_native_env as module
    monkeypatch.setattr(module, "_fast_env_mode", lambda: True)
    with pytest.raises(RunnerError, match="full protocol validation"):
        JsonEventCursor().validate(full_capture())
