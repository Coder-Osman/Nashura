import copy
import json

import pytest

from native_runner.live.snapshot import CaptureReadError, LiveStateError, latest_snapshot, model_blockers, validate_snapshot, validate_touch
from native_runner.live.touch import deploy_once


@pytest.fixture
def snapshot():
    deck = [{"deck_slot": i, "card_id": 26000000 + i} for i in range(8)]
    return {
        "schema": "cr-live-snapshot.v1", "event": "snapshot", "sequence": 1,
        "battle_epoch": 1, "tick": 200, "own_side": 1, "ended": False, "winner": None,
        "received_monotonic_ns": 1_000_000_000,
        "own": {"elixir_raw": 60000, "deck": deck,
                "hand": [{"slot": i, "card": deck[i]} for i in range(4)], "next_card": deck[4]},
        "entities": [{"id": 5000001, "owner": 0, "kind": 15, "card_id": 26000000,
                      "x": 9000, "y": 15000, "hp": 200, "max_hp": 300, "visibility": "visible"}],
        "coverage": {"base_entities": True, "model_ready": False},
    }


def test_fresh_correct_hand_can_be_touched(snapshot):
    assert validate_touch(snapshot, epoch=1, slot=0, card_id=26000000,
                          now_ns=1_100_000_000)["card_id"] == 26000000


@pytest.mark.parametrize("changes,kwargs", [
    ({}, {"now_ns": 2_000_000_000}), ({}, {"now_ns": 900_000_000}),
    ({"ended": True}, {}), ({"battle_epoch": 2}, {}), ({"tick": 100}, {}),
    ({}, {"card_id": 26000007}), ({}, {"slot": -1}),
])
def test_touch_rejects_stale_changed_or_early_state(snapshot, changes, kwargs):
    snapshot.update(changes)
    params = dict(epoch=1, slot=0, card_id=26000000, now_ns=1_100_000_000)
    params.update(kwargs)
    with pytest.raises(LiveStateError):
        validate_touch(snapshot, **params)


def test_private_opponent_and_invisible_entities_are_rejected(snapshot):
    bad = copy.deepcopy(snapshot)
    bad["opponent_hand"] = [26000000]
    with pytest.raises(LiveStateError):
        validate_snapshot(bad)
    snapshot["entities"][0]["visibility"] = "unknown"
    with pytest.raises(LiveStateError):
        validate_snapshot(snapshot)


def test_live_vector_reuse_and_hand_mismatch_are_rejected(snapshot):
    bad = copy.deepcopy(snapshot)
    bad["entities"].append(bad["entities"][0])
    with pytest.raises(LiveStateError):
        validate_snapshot(bad)
    bad = copy.deepcopy(snapshot)
    bad["own"]["hand"][0]["card"] = {"deck_slot": 0, "card_id": 999}
    with pytest.raises(LiveStateError):
        validate_snapshot(bad)


@pytest.mark.parametrize('field', ['cost', 'form_code'])
def test_hand_and_next_selection_must_agree_with_deck(snapshot, field):
    snapshot['own']['deck'][0][field] = 1
    snapshot['own']['hand'][0]['card'] = dict(snapshot['own']['deck'][0], **{field:0})
    with pytest.raises(LiveStateError, match='selection mismatch: '+field):
        validate_snapshot(snapshot)
    snapshot['own']['hand'][0]['card'] = snapshot['own']['deck'][0]
    snapshot['own']['deck'][4][field] = 1
    snapshot['own']['next_card'] = dict(snapshot['own']['deck'][4], **{field:0})
    with pytest.raises(LiveStateError, match='selection mismatch: '+field):
        validate_snapshot(snapshot)


def test_partial_tail_is_ignored_but_stopped_capture_is_not_reused(tmp_path, snapshot):
    p = tmp_path / "capture.jsonl"
    p.write_text(json.dumps(snapshot) + '\n{"event":"snap')
    assert latest_snapshot(p) == snapshot
    p.write_text(json.dumps(snapshot) + '\n{"event":"stopped"}\n')
    with pytest.raises(LiveStateError):
        latest_snapshot(p)


def test_partial_capture_is_not_declared_model_ready(snapshot):
    assert "action_masks" in model_blockers(snapshot)
    assert "atomic_snapshot" in model_blockers(snapshot)


@pytest.mark.parametrize("hp,max_hp", [(2559997, 2559997), (1500, 1000), (1, 0),
                                      (2**31 - 1, 2**31 - 1)])
def test_native_int32_health_is_preserved(snapshot, hp, max_hp):
    snapshot["entities"][0].update(hp=hp, max_hp=max_hp)
    entity = validate_snapshot(snapshot)["entities"][0]
    assert (entity["hp"], entity["max_hp"]) == (hp, max_hp)


@pytest.mark.parametrize("key,value", [("hp", 0), ("hp", -1), ("hp", 2**31),
                                      ("max_hp", -1), ("max_hp", 2**31),
                                      ("hp", True), ("max_hp", 1.5)])
def test_invalid_native_health_still_stops_capture(snapshot, key, value):
    snapshot["entities"][0][key] = value
    with pytest.raises(LiveStateError, match="invalid " + key):
        validate_snapshot(snapshot)


@pytest.mark.parametrize("error", [
    {"event": "read_error", "message": "invalid HP: entity=5000060 max_hp=-1"},
    {"event": "script_error", "detail": {"description": "invalid HP component type"}},
])
def test_capture_error_details_survive_to_runner(tmp_path, snapshot, error):
    path = tmp_path / "capture.jsonl"
    path.write_text(json.dumps(snapshot) + "\n" + json.dumps(error) + "\n")
    reason = error.get("message") or error["detail"]["description"]
    with pytest.raises(LiveStateError) as caught:
        latest_snapshot(path)
    assert str(caught.value) == f"live capture is not ready: {error['event']}: {reason}"
    assert isinstance(caught.value, CaptureReadError) == (error['event'] == 'read_error')


def test_discarded_sample_requires_a_new_valid_snapshot(tmp_path, snapshot):
    path = tmp_path/'capture.jsonl'
    path.write_text(json.dumps(snapshot)+'\n'+json.dumps(dict(event='read_error', message='duplicate entity identity'))+'\n')
    with pytest.raises(CaptureReadError):
        latest_snapshot(path)
    snapshot.update(sequence=2, tick=202)
    with path.open('a') as output:
        output.write(json.dumps(snapshot)+'\n')
    assert latest_snapshot(path)['sequence'] == 2


def test_duplicate_touch_receipt_prevents_any_input(tmp_path, snapshot, monkeypatch):
    capture = tmp_path / "capture.jsonl"
    capture.write_text(json.dumps({"serial": "emulator-5570", "pid": 123}) + "\n" + json.dumps(snapshot) + "\n")
    receipt = tmp_path / "receipt.jsonl"
    receipt.write_text("existing request")
    calls = []

    def output(args, **kwargs):
        calls.append(args)
        return "Physical size: 1080x1920" if args[-2:] == ["wm", "size"] else "123"

    monkeypatch.setattr("native_runner.live.touch.subprocess.check_output", output)
    monkeypatch.setattr("native_runner.live.snapshot.time.monotonic_ns", lambda: 1_100_000_000)
    with pytest.raises(FileExistsError):
        deploy_once(adb=tmp_path / "adb", serial="emulator-5570", capture=capture, receipt=receipt,
                    epoch=1, slot=0, card_id=26000000, screen=(729, 1100))
    assert all("input" not in call for call in calls)
