"""Offline tests of event label provenance and actor-relative capture."""

from types import SimpleNamespace

import pytest

from native_runner.contracts import ActionV1
from native_runner.perspective import PerspectiveTransformV1
from native_runner.training.event_cache import (
    default_event_contract,
    load_event_cache_shard,
    validate_event_supervised_contract,
    write_event_cache_index,
    write_event_cache_shard,
)
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_producer import _EventLaneV1, _capture_trigger, build_event_label
from native_runner.training.v4.expert import TimedExpertActionV4
from native_runner.tests.test_event_producer import _play
from native_runner.tests.test_training_v4_model import _batch


def _lane():
    frames = (_batch(1), _batch(1))
    plays = tuple(
        (_play(102 + owner * 2, owner, 11 + owner, owner, cell),)
        for owner, cell in enumerate(((3, 4), (14, 27)))
    )
    lane = SimpleNamespace(
        current_tick=101,
        observations=(SimpleNamespace(tick=101), SimpleNamespace(tick=101)),
        prepared=SimpleNamespace(replay_tag="mirror"),
        tensorizers=tuple(
            SimpleNamespace(
                perspective=PerspectiveTransformV1(actor_owner=owner),
                tensorize=lambda observation, validate, frame=frames[owner]: frame,
            )
            for owner in (0, 1)
        ),
    )
    return _EventLaneV1(
        lane=lane,
        plays_by_owner=plays,
        actions=plays[0] + plays[1],
        checkpoints=(101,),
        capture_ticks=(101,),
        trigger_events={101: ((0, plays[0][0]), (1, plays[1][0]))},
        config=EventPolicyConfigV1(),
    )


def test_same_tick_labels_are_cached_independently_by_owner():
    lane = _lane()
    for owner in (1, 0):
        label = lane.label(101, owner)
        assert label.owner == owner
        assert label.card_id == 11 + owner
        assert label.target_tick == 102 + owner * 2
        assert lane.label(101, owner) is label
    assert set(lane.label_cache) == {(0, 101), (1, 101)}


def test_mirrored_capture_converts_once_and_keeps_cached_labels_native():
    lane = _lane()
    assert lane.label(101, 1).target_cell == (14, 27)
    _capture_trigger(lane, 101)
    # Both simultaneous triggers reuse each owner's native label, without a
    # second flip as records are emitted for the second trigger.
    assert [record.owner for record in lane.records] == [0, 1, 0, 1]
    assert [record.target_cell for record in lane.records] == [(3, 4)] * 4
    assert lane.label(101, 1).target_cell == (14, 27)
    assert lane.label(101, 0).target_cell == (3, 4)
    assert lane.records[1].card_id == 12
    assert lane.records[1].hand_slot == 1


def test_capture_uses_tensorizer_perspective():
    lane = _lane()
    lane.lane.tensorizers[1].perspective = PerspectiveTransformV1(1, horizontal_mirror=True)
    _capture_trigger(lane, 101)
    assert lane.records[1].target_cell == (14, 4)


def test_missing_target_is_wait_with_valid_trigger_tick():
    action = TimedExpertActionV4(source_tick=102, action=ActionV1.wait(1), source_index=0)
    label = build_event_label((action,), owner=1, event_tick=101)
    assert label.wait and label.target_cell is None
    assert label.trigger_tick == 101


def test_capture_wait_does_not_transform_target():
    lane = _lane()
    lane.plays_by_owner = ((), ())
    _capture_trigger(lane, 101)
    assert all(record.wait and record.target_cell is None for record in lane.records)


@pytest.mark.parametrize("legacy", [False, True])
def test_builder_resume_checks_shard_contract_without_rewriting(tmp_path, monkeypatch, legacy):
    from tools import build_event_observations as builder

    lane = _lane()
    _capture_trigger(lane, 101)
    contract = {"version": "event-observation-cache-contract.v1"} if legacy else default_event_contract()
    shard_id = "part-00-shard-000000"
    data_path = tmp_path / f"{shard_id}.crev.zst"
    write_event_cache_shard(data_path, lane.records, lane.observations, shard_id=shard_id, contract=contract)
    original = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    monkeypatch.setattr(builder, "NativeClashEnv", lambda **kwargs: SimpleNamespace(
        wait_ready=lambda **kwargs: None, stop_resident_mode=lambda: None,
    ))
    def unexpected_capture(**kwargs):
        pytest.fail("resume must not invoke capture")
    monkeypatch.setattr(builder, "_convert_batch", unexpected_capture)
    kwargs = dict(
        port=1, host="unused", chunks=[(0, [])], cache_root=str(tmp_path),
        contract=default_event_contract(), timeout=1, capture_mode="unused",
        compression_level=-5, partition_index=0,
    )
    if legacy:
        with pytest.raises(ValueError, match="version"):
            builder._run_port(**kwargs)
    else:
        summaries, failures, stats = builder._run_port(**kwargs)
        assert len(summaries) == 1 and not failures and not stats
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == original



@pytest.mark.parametrize("contract", [None, {}, {"test": True}, {"version": "event-observation-cache-contract.v1"}])
def test_supervision_refuses_unknown_or_legacy_contract(contract):
    with pytest.raises(ValueError, match="contract"):
        validate_event_supervised_contract(contract)


@pytest.mark.parametrize("field,value", [
    ("version", "event-observation-cache-contract.v1"),
    ("action_cell_frame", "native"),
    ("input_semantics", "absolute-owner"),
])
def test_supervision_requires_all_semantic_fields(field, value):
    contract = default_event_contract()
    contract[field] = value
    with pytest.raises(ValueError, match=field):
        validate_event_supervised_contract(contract)
    del contract[field]
    with pytest.raises(ValueError, match=field):
        validate_event_supervised_contract(contract)


@pytest.mark.parametrize("contract", [default_event_contract(), {"test": True}, {"version": "event-observation-cache-contract.v1"}])
def test_contract_serialization_preserves_provenance_and_never_flips(tmp_path, contract):
    lane = _lane()
    _capture_trigger(lane, 101)
    summary = write_event_cache_shard(
        tmp_path / "labels.crev.zst", lane.records, lane.observations,
        shard_id="labels", contract=contract,
    )
    loaded = load_event_cache_shard(tmp_path / "labels.json", verify=True)
    assert loaded.manifest["contract"] == contract
    assert loaded.records == tuple(lane.records)
    assert loaded.records[1].target_cell == (3, 4)
    index = write_event_cache_index(tmp_path / "index.json", [summary], contract=contract)
    assert index["contract"] == contract
    if contract == default_event_contract():
        assert contract["version"] == "event-observation-cache-contract.v2"
        assert contract["action_cell_frame"] == "actor_relative.v1"
        assert contract["input_semantics"] == "owner-neutral.v1"
        validate_event_supervised_contract(loaded.manifest["contract"])
    else:
        with pytest.raises(ValueError, match="contract"):
            validate_event_supervised_contract(loaded.manifest["contract"])
