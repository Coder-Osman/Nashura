"""Behavior provenance is checked independently of matching action scores."""
from dataclasses import replace

import pytest

from native_runner.tests.test_event_preflight import rollout  # noqa: F401
from native_runner.training.event_cache import load_event_rl_shard, write_event_rl_shard
from native_runner.training.event_preflight import EventPreflightError, load_complete_rollout

IDENTITY = dict(behavior_checkpoint_sha256="a" * 64,
                behavior_model_contract_hash="b" * 64)
EXPECTED = dict(expected_checkpoint_sha256="a" * 64,
                expected_model_contract_hash="b" * 64)


def bind(summary, *, bad_field=None, location=None, value=None):
    shard = load_event_rl_shard(summary)
    contract = dict(IDENTITY)
    records = [replace(row, **IDENTITY) for row in shard.records]
    if location == "contract":
        if value is None:
            contract.pop(bad_field)
        else:
            contract[bad_field] = value
    elif location == "row":
        records[-1] = replace(records[-1], **{bad_field: value})
    write_event_rl_shard(
        shard.path, records, list(shard.observations()), shard_id="identity", contract=contract,
        masks=list(zip(shard.card_masks, shard.cell_masks, shard.delay_masks, shard.gate_masks)),
    )


def test_matching_identity_accepts_every_row(rollout):
    _, summary = rollout
    bind(summary)
    lanes, counts = load_complete_rollout([summary], **EXPECTED)
    assert counts["rows"] == sum(lane.time_steps for lane in lanes) == 5


@pytest.mark.parametrize("field", list(IDENTITY))
@pytest.mark.parametrize("location", ["contract", "row"])
@pytest.mark.parametrize("value", [None, "c" * 64])
def test_missing_or_wrong_identity_rejects_before_assembly(rollout, monkeypatch, field, location, value):
    _, summary = rollout
    bind(summary, bad_field=field, location=location, value=value)
    def forbidden(*args, **kwargs):
        pytest.fail("identity mismatch reached lane assembly")
    monkeypatch.setattr("native_runner.training.event_preflight.lanes_from_shard", forbidden)
    with pytest.raises(EventPreflightError, match=field):
        load_complete_rollout([summary], **EXPECTED)


def test_legacy_identity_optional_for_read_but_not_training(rollout):
    _, summary = rollout
    assert load_complete_rollout([summary])[1]["rows"] == 5
    with pytest.raises(EventPreflightError, match="identity"):
        load_complete_rollout([summary], **EXPECTED)


@pytest.mark.parametrize("owner, expected_owners", [(None, {0, 1}), (0, {0})])
def test_recorder_binds_mirror_and_learner_only_shards(tmp_path, owner, expected_owners):
    from native_runner.tests.test_event_rollout import _decision, DECKS, TEMPERATURES
    from native_runner.training.event_rollout import EventLaneRecorderV1
    recorder = EventLaneRecorderV1(match_id="owned", policy_version="same-label",
                                   temperatures=TEMPERATURES, decks=DECKS, **IDENTITY)
    for actor in (0, 1):
        _decision(recorder, actor, 100)
    recorder.advance(ticks=10, done=True, rewards={0: 1., 1: -1.})
    recorder.to_shard(tmp_path / "owned.crev.zst", shard_id="owned", contract={}, owner=owner)
    shard = load_event_rl_shard(tmp_path / "owned.json", verify=True)
    assert {row.owner for row in shard.records} == expected_owners
    from native_runner.training.event_rollout import EXECUTION_CREDIT_CONTRACT
    assert shard.manifest["contract"] == {**IDENTITY, "execution_credit_contract": EXECUTION_CREDIT_CONTRACT}
    assert all(all(getattr(row, key) == value for key, value in IDENTITY.items()) for row in shard.records)
    lanes, _ = load_complete_rollout([tmp_path / "owned.json"], **EXPECTED)
    assert len(lanes) == len(expected_owners)
    with pytest.raises(ValueError, match="disagrees"):
        recorder.to_shard(tmp_path / "wrong.crev.zst", shard_id="wrong",
                          contract={"behavior_checkpoint_sha256": "wrong"})
