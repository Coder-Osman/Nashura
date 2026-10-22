"""Round-trip and validation tests for rollout shards."""

from __future__ import annotations

import pytest
import torch

from native_runner.training.event_cache import (
    EventObservationRecordV1,
    EventRlRecordV1,
    load_event_cache_shard,
    load_event_rl_shard,
    write_event_cache_shard,
    write_event_rl_shard,
)
from native_runner.tests.test_training_v4_model import _batch

DECK = (9004, 1002, 7007, 3003, 5005, 8008, 6006, 2001)
OPPONENT_DECK = (9005, 1003, 7008, 3004, 5006, 8009, 6007, 2002)


def _contract() -> dict[str, object]:
    return {"version": "test", "delay_offset_ms": [0, 100], "storage_float_dtype": "float16"}


def _record(*, act: bool = True, reward: float = 0.0, done: bool = False, owner: int = 0) -> EventRlRecordV1:
    return EventRlRecordV1(
        match_id="m-1",
        owner=owner,
        observation_tick=200,
        decision_ticks=14,
        act=act,
        card_row=3 if act else -1,
        cell=12 * 18 + 5 if act else -1,
        delay_bin=7 if act else -1,
        log_prob=-3.5,
        gate_log_prob=-0.4,
        card_log_prob=-1.1 if act else 0.0,
        cell_log_prob=-1.5 if act else 0.0,
        delay_log_prob=-0.5 if act else 0.0,
        gate_entropy=0.6,
        card_entropy=1.7 if act else 0.0,
        cell_entropy=5.2 if act else 0.0,
        delay_entropy=3.1 if act else 0.0,
        temperatures=(1.0, 1.2, 0.8, 1.0),
        reward=reward,
        done=done,
        truncated=False,
        policy_version="ckpt-a",
        deck=DECK,
        opponent_deck=OPPONENT_DECK,
    )


def test_rl_shard_round_trip(tmp_path) -> None:
    records = [_record(), _record(act=False), _record(reward=1.4, done=True)]
    observations = [_batch(1) for _ in range(3)]
    masks = [
        (
            torch.ones(8, dtype=torch.bool),
            torch.ones(8, 576, dtype=torch.bool),
            torch.ones(8, 61, dtype=torch.bool),
            torch.ones(2, dtype=torch.bool),
        )
        for _ in records
    ]
    destination = tmp_path / "part-00-shard-000001.crev.zst"

    summary = write_event_rl_shard(
        destination,
        records,
        observations,
        shard_id="part-00-shard-000001",
        contract=_contract(),
        masks=masks,
    )
    assert summary["frames"] == 3
    assert summary["act_frames"] == 2
    assert summary["wait_frames"] == 1
    assert summary["rewarded_frames"] == 1
    assert summary["terminal_frames"] == 1
    assert summary["matches"] == 1
    assert summary["policies"] == ["ckpt-a"]

    shard = load_event_rl_shard(tmp_path / "part-00-shard-000001.json", verify=True)
    assert shard.frames == 3
    assert tuple(shard.card_masks.shape) == (3, 8)
    assert tuple(shard.cell_masks.shape) == (3, 8, 576)
    assert tuple(shard.delay_masks.shape) == (3, 8, 61)
    assert tuple(shard.gate_masks.shape) == (3, 2)
    assert [item.as_dict for item in shard.records] == [item.as_dict for item in records]
    assert shard.observations_batch().batch_size == 3
    # The dynamic capacities are per shard, so a row must still restore alone.
    assert shard.observation(1).batch_size == 1
    assert [item.batch_size for item in shard.observations()] == [1, 1, 1]


def test_rl_shard_carries_observation_content(tmp_path) -> None:
    """The stored frame must survive the fp16 packing, not just its shape."""

    observation = _batch(1)
    destination = tmp_path / "shard.crev.zst"
    write_event_rl_shard(
        destination,
        [_record()],
        [observation],
        shard_id="shard",
        contract=_contract(),
    )
    restored = load_event_rl_shard(tmp_path / "shard.json").observation(0)
    reference = observation.narrow_batch(0, 1)
    assert restored.own_cards.card_vocab_id.tolist() == reference.own_cards.card_vocab_id.tolist()
    assert restored.match_scalars.shape == reference.match_scalars.shape
    # fp16 storage: values come back close, not bit-identical.
    assert bool(
        (restored.match_scalars.to(reference.match_scalars.dtype) - reference.match_scalars).abs().max() < 1e-2
    )


def test_canonical_frame_exactly_matches_real_fp16_shard(tmp_path) -> None:
    from native_runner.training.event_cache import canonicalize_event_observation
    from native_runner.training.v4.cache import _flatten_record

    raw = _batch(1)
    raw.match_scalars.fill_(0.1234567)
    raw.candidates.elixir.fill_(4.999)
    raw.candidates.cost.fill_(2.001)
    canonical = canonicalize_event_observation(raw)
    assert not torch.equal(raw.match_scalars, canonical.match_scalars)
    assert torch.equal(raw.candidates.elixir, canonical.candidates.elixir)
    assert torch.equal(raw.candidates.cost, canonical.candidates.cost)
    record = _record()
    write_event_rl_shard(
        tmp_path / "canonical.crev.zst", [record], [canonical],
        shard_id="canonical", contract=_contract(),
    )
    shard = load_event_rl_shard(tmp_path / "canonical.json", verify=True)
    restored = shard.observation(0).to_model_input("cpu", float_dtype=torch.float32)
    expected, actual = {}, {}
    _flatten_record(canonical, prefix="obs", destination=expected)
    _flatten_record(restored, prefix="obs", destination=actual)
    assert expected.keys() == actual.keys()
    for name in expected:
        assert torch.equal(expected[name], actual[name]), name
    assert shard.records[0].as_dict == record.as_dict


def test_wait_rows_may_not_carry_head_terms() -> None:
    with pytest.raises(ValueError):
        EventRlRecordV1(**{**_record(act=False).as_dict, "card_row": 2})
    with pytest.raises(ValueError):
        EventRlRecordV1(**{**_record(act=False).as_dict, "cell_log_prob": -0.1})
    with pytest.raises(ValueError):
        EventRlRecordV1(**{**_record(act=False).as_dict, "gate_entropy": 0.0, "delay_entropy": 2.0})


def test_invalid_rows_are_rejected() -> None:
    base = _record().as_dict
    with pytest.raises(ValueError):
        EventRlRecordV1(**{**base, "act": True, "delay_bin": -1})
    with pytest.raises(ValueError):
        EventRlRecordV1(**{**base, "temperatures": (1.0, 1.0, 1.0)})
    with pytest.raises(ValueError):
        EventRlRecordV1(**{**base, "temperatures": (1.0, 0.0, 1.0, 1.0)})
    with pytest.raises(ValueError):
        EventRlRecordV1(**{**base, "deck": DECK[:7]})
    with pytest.raises(ValueError):
        EventRlRecordV1(**{**base, "done": True, "truncated": True})
    with pytest.raises(ValueError):
        EventRlRecordV1(**{**base, "log_prob": float("nan")})


def test_il_label_shards_still_round_trip(tmp_path) -> None:
    """Regression for the container refactor: the IL path must be unchanged."""

    record = EventObservationRecordV1(
        replay_tag="r",
        owner=0,
        observation_tick=200,
        trigger_event_id="e",
        trigger_owner=1,
        trigger_card_id=7,
        wait=False,
        card_id=int(DECK[3]),
        hand_slot=0,
        target_cell=(5, 12),
        delay_ms=700,
        delay_bin=7,
        target_tick=214,
        next_trigger_tick=None,
        terminal=False,
        candidate_index=0,
        candidate_uid=101,
        legal_at_trigger=True,
        card_in_hand=True,
    )
    destination = tmp_path / "il.crev.zst"
    summary = write_event_cache_shard(
        destination,
        [record],
        [_batch(1)],
        shard_id="il",
        contract=_contract(),
    )
    assert summary["act_frames"] == 1
    assert summary["frames_in_hand"] == 1
    shard = load_event_cache_shard(tmp_path / "il.json", verify=True)
    assert shard.frames == 1
    assert shard.records[0].as_dict == record.as_dict
    assert shard.observation(0).batch_size == 1
