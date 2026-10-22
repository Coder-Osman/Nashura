"""Event-driver restore tests for receipts, cancellation and timing boundaries."""
import json
import math
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import torch

from event_policy_rollout import (
    EventPolicyRollout, SearchExecutionLedger, _STATE_FIELDS, action_key, decode_action,
)
from native_runner.contracts import ActionV1
from native_runner.tests.test_event_ability_actions import ability_frame, ability_model
from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_event_rollout import _decision, _recorder
from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.event_scheduling import EventExecutionLedger
from native_runner.training.v4.model import _distribution
from policy_rollout import _EPISODE_FIELDS, _TRACKER_FIELDS
from policy_rollout import _decode, _encode


def driver():
    result = EventPolicyRollout.__new__(EventPolicyRollout)
    for name in _STATE_FIELDS:
        setattr(result, name, {})
    result.recorder = _recorder()
    result.ledger = EventExecutionLedger()
    result.initial_tower_hp = {0: 4824, 1: 3052}
    result.cancellations = 0
    result.ready = []
    result.tick = 100
    result.terminal = False
    result.device = torch.device("cpu")
    result.started = True
    result._search_execution_ledger = None
    result._search_recorder_capsule = None
    result.profile_sink = None
    result.profile_counters = {}
    result.environment = SimpleNamespace(native=SimpleNamespace(observe=lambda: None))
    result.overflow = None
    result.tensorizers = {
        owner: SimpleNamespace(**{name: None for name in _EPISODE_FIELDS},
            tracker=SimpleNamespace(**{name: {} for name in _TRACKER_FIELDS}))
        for owner in (0, 1)
    }
    return result


@pytest.mark.parametrize("key", ["wait", "play:2:13:4", "ability:3:-1", "ability:3:7"])
def test_action_identity_roundtrip(key):
    assert action_key(decode_action(key, device=torch.device("cpu"))) == key


@pytest.mark.parametrize("shape", [(1, 0, 7), (0, 2, 0), (1, 3, 0, 5)])
def test_json_restore_preserves_empty_snapshot_tensor_dimensions(shape):
    tensor = torch.empty(shape, dtype=torch.float32)
    restored = _decode(json.loads(json.dumps(_encode(tensor))))
    assert restored.shape == tensor.shape
    assert restored.dtype == tensor.dtype


@pytest.mark.parametrize("key", ["play:-1:0:0", "play:0:0:-1", "ability:-1:0", "ability:0:-2", "wait:1"])
def test_action_identity_rejects_invalid_coordinates(key):
    with pytest.raises(ValueError):
        decode_action(key, device=torch.device("cpu"))


@pytest.mark.parametrize("closed", [False, True])
def test_json_restore_delivers_receipt_to_open_or_closed_row(closed):
    result = driver()
    _decision(result.recorder, 0, 100, executed=False)
    result.recorder.submit(0, tick=100, action_id="test:0:100")
    if closed:
        result.recorder.advance(ticks=2, rewards={0: .2})
        _decision(result.recorder, 0, 102, act=False)
    encoded = json.loads(json.dumps(result.capture()))
    expected_rng = torch.rand(10)
    result.initial_tower_hp.clear()
    result.restore(encoded)
    assert torch.equal(torch.rand(10), expected_rng)
    assert result.initial_tower_hp == {0: 4824, 1: 3052}
    result.recorder.reconcile([SimpleNamespace(owner=0, event_type="action_executed",
        data={"action_id": "test:0:100"})])
    if closed:
        assert result.recorder.rows[0][0].execution_status == "executed"
        assert result.recorder.rows[0][0].reward == .2
    else:
        assert result.recorder._pending[0].execution_status == "executed"
    assert not result.recorder._submitted
    result.restore(encoded)
    assert "test:0:100" in result.recorder._submitted


def test_search_restore_delivers_receipt_without_json_roundtrip():
    result = driver()
    _decision(result.recorder, 0, 100, executed=False)
    result.recorder.submit(0, tick=100, action_id="test:0:100")
    torch.manual_seed(31)
    snapshot = result.capture_search()
    expected_rng = torch.rand(10)
    result.initial_tower_hp.clear()
    result.restore_search(snapshot)
    assert torch.equal(torch.rand(10), expected_rng)
    assert result.initial_tower_hp == {0: 4824, 1: 3052}
    result.recorder.reconcile([SimpleNamespace(owner=0, event_type="action_executed",
        data={"action_id": "test:0:100"})])
    assert result.recorder._pending[0].execution_status == "executed"


def test_cancel_is_counted_once_and_never_counts_wait():
    result = driver()
    _decision(result.recorder, 0, 100, executed=False)
    result._cancel(0, "interrupted")
    result._cancel(0, "gameplay_end")
    result._cancel(1, "gameplay_end")
    _decision(result.recorder, 0, 101, act=False)
    result._cancel(0, "gameplay_end")
    assert result.cancellations == 1


def test_search_wait_commit_skips_model_and_training_recorder():
    result = driver()
    result.ready = [0]
    result.legals = {0: SimpleNamespace()}
    result.plans = {0: None, 1: None}
    result.recovery_owners = set()
    result.last_decision_by_owner = {0: 100, 1: 100}
    result.decisions = 0
    result.event_ticks = []
    result._search_execution_ledger = SearchExecutionLedger([], {}, {}, set())
    result.profile_counters = {}
    rows_before = {owner: len(rows) for owner, rows in result.recorder.rows.items()}

    result.commit_search_action(0, "wait")

    assert result.ready == []
    assert result.recorder.rows.keys() == rows_before.keys()
    assert {owner: len(rows) for owner, rows in result.recorder.rows.items()} == rows_before
    assert result._search_execution_ledger.entries[0].status == "executed"
    assert "model_evaluate_action_calls" not in result.profile_counters


def test_search_execution_ledger_tracks_cancel_submit_and_late_receipt():
    ledger = SearchExecutionLedger([], {}, {}, set())
    ledger.commit(0, 100, "play:1:3:2", 102)
    assert ledger.cancel(0, "cancel_before_submit")
    assert ledger.entries[0].status == "cancelled"
    assert not ledger.cancel(0, "duplicate_cancel")

    ledger.commit(0, 104, "play:1:3:2", 106)
    action = ActionV1.play(0, 1, (3, 4), card_id=7, execute_offset_ticks=1,
                           action_id="match:0:0")
    ledger.submit(0, 104, action)
    ledger.current_by_owner.pop(0)
    ledger.entries[1].closed_ref = True
    ledger.reconcile([SimpleNamespace(owner=0, event_type="action_rejected",
        data={"action_id": "match:0:0"})])

    assert ledger.entries[1].status == "cancelled"
    assert ledger.entries[1].cancel_reason == "action_rejected"
    assert ledger.entries[1].closed_ref
    assert not ledger.submitted


def test_search_execution_ledger_restore_preserves_late_receipt_without_rows():
    result = driver()
    ledger = SearchExecutionLedger([], {}, {}, set())
    ledger.commit(0, 100, "play:1:3:2", 102)
    action = ActionV1.play(0, 1, (3, 4), card_id=7, execute_offset_ticks=1,
                           action_id="match:0:0")
    ledger.submit(0, 100, action)
    result._search_execution_ledger = ledger
    result.tensorizers = {
        owner: SimpleNamespace(**{name: None for name in _EPISODE_FIELDS},
            tracker=SimpleNamespace(**{name: {} for name in _TRACKER_FIELDS}))
        for owner in (0, 1)
    }
    snapshot = result.capture_search()
    json.dumps(snapshot, allow_nan=False)
    result._search_execution_ledger.entries.clear()
    result.restore_search(snapshot)
    result._search_execution_ledger.reconcile([SimpleNamespace(
        owner=0, event_type="action_rejected", data={"action_id": "match:0:0"})])

    assert result._search_execution_ledger.entries[0].status == "cancelled"
    assert result._search_execution_ledger.entries[0].closed_ref
    assert not result._search_execution_ledger.submitted
    assert not result.recorder.rows[0]
    assert not result.recorder.rows[1]


@pytest.mark.parametrize("stop_tick", [99, 100])
def test_native_advance_rejects_empty_horizon_before_touching_engine(stop_tick):
    with pytest.raises(ValueError, match="later"):
        driver().step_native(stop_tick=stop_tick)


def test_policy_prior_batches_play_scores_without_changing_joint_masses():
    torch.manual_seed(23)
    model = _event_model().eval()
    card_mask = torch.zeros(model.card_count, dtype=torch.bool)
    card_mask[:2] = True
    cell_masks = torch.zeros(model.card_count, model.cell_count, dtype=torch.bool)
    cell_masks[0, [0, 2]] = True
    cell_masks[1, [3, 5]] = True
    delay_masks = torch.zeros(model.card_count, model.event_config.delay_bin_count, dtype=torch.bool)
    delay_masks[0, [0, 1]] = True
    delay_masks[1, [1, 3]] = True
    result = driver()
    result.ready = [0]
    result.models = {0: model}
    result.frames = {0: _batch(1)}
    result.legals = {0: SimpleNamespace(
        act_allowed=True, card_mask=card_mask, cell_masks=cell_masks,
        delay_masks=delay_masks, legal_rows=(0, 1),
    )}
    result.config = model.event_config
    result.temperatures = (1.0, 1.0, 1.0, 1.0)

    with patch.object(model, "_cell_conditioning", wraps=model._cell_conditioning) as condition:
        with torch.inference_mode():
            actual = result.policy_prior(0)
    assert condition.call_count == 1
    assert condition.call_args.args[1].tolist() == [0, 1]

    with torch.inference_mode():
        context = result.context(0)
        masks = result.masks(0)
        gate_scores, gate_mask = model.event_gate(context, masks["gate_mask"])
        gate_log = _distribution(gate_scores, mask=gate_mask, temperature=1.0, validate=False).logits[0]
        card_log = _distribution(model.card_logits(context), mask=masks["card_mask"],
                                 temperature=1.0, validate=False).logits[0]
        log_probs = {"wait": float(gate_log[0])}
        for row in result.legals[0].legal_rows:
            row_tensor = torch.tensor([row])
            cell_log = _distribution(model.cell_logits(context, row_tensor),
                                     mask=cell_masks[row].unsqueeze(0),
                                     temperature=1.0, validate=False).logits[0]
            for cell in cell_masks[row].nonzero(as_tuple=False).flatten().tolist():
                delay_log = model.action_delay_distribution(
                    context, row_tensor, torch.tensor([cell]), torch.tensor([-1]),
                    masks["delay_mask"], 1.0, False,
                ).logits[0]
                for delay in delay_masks[row].nonzero(as_tuple=False).flatten().tolist():
                    log_probs[f"play:{row}:{cell}:{delay}"] = float(
                        gate_log[1] + card_log[row] + cell_log[cell] + delay_log[delay]
                    )
        maximum = max(log_probs.values())
        expected = {key: math.exp(value - maximum) for key, value in log_probs.items()}

    assert actual.keys() == expected.keys()
    assert actual == pytest.approx(expected, abs=1e-6)


def test_policy_prior_keeps_ability_and_play_actions():
    model = ability_model()
    card_mask = torch.zeros(model.card_count, dtype=torch.bool)
    card_mask[0] = True
    cell_masks = torch.zeros(model.card_count, model.cell_count, dtype=torch.bool)
    cell_masks[0, 2] = True
    delay_masks = torch.zeros(model.card_count, model.event_config.delay_bin_count, dtype=torch.bool)
    delay_masks[0, [0, 1]] = True
    result = driver()
    result.ready = [0]
    result.models = {0: model}
    result.frames = {0: ability_frame()}
    result.legals = {0: SimpleNamespace(
        act_allowed=True, card_mask=card_mask, cell_masks=cell_masks,
        delay_masks=delay_masks, legal_rows=(0,),
    )}
    result.config = model.event_config
    result.temperatures = (1.0, 1.0, 1.0, 1.0)

    prior = result.policy_prior(0)

    assert set(prior) == {"wait", "play:0:2:0", "play:0:2:1", "ability:2:-1"}
    assert all(math.isfinite(value) and value >= 0 for value in prior.values())
