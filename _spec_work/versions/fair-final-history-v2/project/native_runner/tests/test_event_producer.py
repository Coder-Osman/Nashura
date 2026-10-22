"""Contract tests for event-boundary observation capture and labels."""

from __future__ import annotations

import json
from pathlib import Path

import torch

from native_runner.contracts import ActionKind, ActionV1
from native_runner.training.event_cache import (
    EventObservationRecordV1,
    load_event_cache_shard,
    write_event_cache_shard,
)
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_producer import (
    build_event_label,
    quantize_delay_ms,
    resolve_event_candidate,
)
from native_runner.training.v4.expert import TimedExpertActionV4
from native_runner.tests.test_training_v4_model import _batch


def _play(tick: int, owner: int, card_id: int, slot: int, grid: tuple[int, int]) -> TimedExpertActionV4:
    return TimedExpertActionV4(
        source_tick=tick,
        action=ActionV1.play(owner, slot, grid, card_id=card_id, action_id=f"a{tick}"),
        source_index=tick,
    )


def test_quantize_delay_ms_rounds_to_100ms_and_clamps() -> None:
    assert quantize_delay_ms(0) == 0
    assert quantize_delay_ms(1) == 0  # 50ms is exactly halfway; banker rounding keeps 0
    assert quantize_delay_ms(2) == 100
    assert quantize_delay_ms(3) == 200  # 150ms rounds to the even 200ms bin
    assert quantize_delay_ms(20) == 1000
    assert quantize_delay_ms(10_000) == 6000


def test_event_label_never_predicts_the_trigger_play() -> None:
    # Owner 0 plays at 100 (the trigger, observable at 101) and next at 161.
    plays = (_play(100, 0, 11, 0, (3, 4)), _play(161, 0, 12, 1, (5, 6)))
    label = build_event_label(plays, owner=0, event_tick=101)
    assert not label.wait
    assert label.card_id == 12
    assert label.target_tick == 161
    assert label.delay_ms == quantize_delay_ms(60) == 3000
    assert label.delay_bin == EventPolicyConfigV1().delay_to_bin(3000)


def test_event_label_waits_when_next_play_is_beyond_horizon() -> None:
    plays = (_play(100, 0, 11, 0, (3, 4)), _play(101 + 121, 0, 12, 1, (5, 6)))
    label = build_event_label(plays, owner=0, event_tick=101)
    assert label.wait
    assert label.card_id is None and label.target_tick is None
    assert label.delay_ms == 0 and label.delay_bin == 0


def test_event_label_waits_without_any_future_play() -> None:
    plays = (_play(100, 0, 11, 0, (3, 4)),)
    label = build_event_label(plays, owner=0, event_tick=101)
    assert label.wait


def test_resolve_event_candidate_binds_hand_slot_and_card() -> None:
    batch = _batch(1)
    candidates = batch.candidates
    card_id = int(candidates.native_visible_card_id[0, 0].item())
    slot = int(candidates.native_hand_slot[0, 0].item())
    from native_runner.training.event_producer import EventLabelV1

    act = EventLabelV1(
        owner=0,
        trigger_tick=100,
        wait=False,
        card_id=card_id,
        hand_slot=slot,
        target_cell=(3, 4),
        delay_ms=100,
        delay_bin=1,
        target_tick=102,
    )
    match = resolve_event_candidate(batch, act)
    assert match.legal_at_trigger
    assert match.card_in_hand
    assert match.candidate_index == 0
    assert match.candidate_uid == 101


def test_event_cache_roundtrip(tmp_path: Path) -> None:
    batch_a = _batch(1).narrow_batch(0, 1)
    batch_b = _batch(1).narrow_batch(0, 1)
    records = (
        EventObservationRecordV1(
            replay_tag="r1",
            owner=0,
            observation_tick=100,
            trigger_event_id="e1",
            trigger_owner=1,
            trigger_card_id=7,
            wait=False,
            card_id=11,
            hand_slot=0,
            target_cell=[3, 4],
            delay_ms=300,
            delay_bin=3,
            target_tick=106,
            next_trigger_tick=None,
            terminal=False,
            candidate_index=0,
            candidate_uid=101,
            legal_at_trigger=True,
            card_in_hand=True,
        ),
        EventObservationRecordV1(
            replay_tag="r1",
            owner=1,
            observation_tick=100,
            trigger_event_id="e1",
            trigger_owner=1,
            trigger_card_id=7,
            wait=True,
            card_id=None,
            hand_slot=None,
            target_cell=None,
            delay_ms=0,
            delay_bin=0,
            target_tick=None,
            next_trigger_tick=None,
            terminal=False,
            candidate_index=-1,
            candidate_uid=-1,
            legal_at_trigger=False,
            card_in_hand=False,
        ),
    )
    summary = write_event_cache_shard(
        tmp_path / "part-00-shard-000000.crev.zst",
        records,
        (batch_a, batch_b),
        shard_id="part-00-shard-000000",
        contract={"test": True},
        compression_level=-5,
    )
    assert summary["frames"] == 2
    assert summary["act_frames"] == 1
    assert summary["legal_act_frames"] == 1
    assert records[0].target_cell == (3, 4)

    loaded = load_event_cache_shard(tmp_path / "part-00-shard-000000.json", verify=True)
    assert loaded.frames == 2
    assert loaded.records[0].card_id == 11
    assert loaded.records[1].wait is True
    restored = loaded.observation(0)
    assert restored.batch_size == 1
    assert restored.candidates.mask.shape[1] == batch_a.candidates.mask.shape[1]
    assert int(restored.candidates.uid[0, 0].item()) == int(batch_a.candidates.uid[0, 0].item())
