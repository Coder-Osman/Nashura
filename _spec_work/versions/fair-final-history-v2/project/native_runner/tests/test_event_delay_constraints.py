"""The policy floor changes action support without changing checkpoint shape."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_event_action_space import (
    _catalog, _deck, _environment, _frame, _grid, _observation, _placement,
)
from native_runner.tests.test_event_model import _event_model, _batch
from native_runner.tests.test_event_preflight import rollout
from native_runner.training.event_action_space import legal_event_actions
from native_runner.training.event_cache import load_event_rl_shard, write_event_rl_shard
from native_runner.training.event_delay_constraints import playable_delay_mask, delay_constraints_contract
from native_runner.training.event_preflight import EventPreflightError, load_complete_rollout, preflight_event_rollout


def test_floor_includes_100_and_6000_and_excludes_both_legacy_bins():
    assert playable_delay_mask((0, 50, 100, 150, 6000)) == (False, False, True, True, True)
    assert playable_delay_mask((0, 50, 100, 150, 6000), minimum_delay_ms=151) == (False,) * 4 + (True,)
    assert playable_delay_mask((0, 50, 100, 6000), tick=5997, end_tick=6000) == (False, False, True, False)


@pytest.mark.parametrize("bins,tick,allowed", [
    ((0, 50), 0, False), ((), 0, False), ((0, 50, 100, 6000), 0, True),
    ((0, 50, 100), 5998, False), ((0, 50, 100), 5997, True),
])
def test_empty_delay_support_disables_card_and_act(bins, tick, allowed):
    catalog, deck = _catalog(), _deck()
    environment = _environment({"0": _placement(_grid([(1, 1)]))})
    observation = _observation([deck[0]])
    observation.tick = tick
    legal = legal_event_actions(environment, observation, _frame(catalog, deck), 0,
        catalog=catalog, card_count=8, cell_count=576, board_width=18, board_height=32,
        delay_offset_ms=bins)
    assert legal.act_allowed == allowed
    assert legal.playable_now == allowed
    assert bool(legal.card_mask.any()) == allowed
    assert bool(legal.cell_masks.any()) == allowed
    assert bool(legal.delay_masks.any()) == allowed
    if not allowed:
        assert (legal.hand_index == -1).all()


@pytest.mark.parametrize("sample", [False, True])
def test_zero_logit_dominance_is_masked_before_sampling_and_rescoring(sample):
    model = _event_model().eval()
    with torch.no_grad():
        model.delay_head[-1].bias[0] = 10000
        context = model.encode(_batch(16), model.initial_state(16))
        mask = torch.tensor(playable_delay_mask(model.event_config.delay_offset_ms)).expand(16, 8, -1)
        kwargs = dict(gate_mask=torch.tensor([[False, True]]).expand(16, -1), delay_mask=mask)
        selected = model.select_action(context, sample=sample, **kwargs)
        replay = model.evaluate_actions(context, selected.action, **kwargs)
    assert selected.action.act.all()
    assert (selected.action.delay_bin >= 1).all()
    assert model.delay_head[-1].weight.shape[0] == 61
    torch.testing.assert_close(selected.log_prob, replay.log_prob)


def rewrite_constraints(summary, constraints, *, corrupt=None):
    shard = load_event_rl_shard(summary)
    records = list(shard.records)
    masks = shard.delay_masks.clone()
    if corrupt == "action":
        records = [replace(row, delay_bin=0) if row.act else row for row in records]
    if corrupt == "mask":
        masks[..., 0] = True
    write_event_rl_shard(shard.path, records, list(shard.observations()), shard_id="part-000",
        contract={**shard.manifest["contract"], "action_constraints": constraints},
        masks=list(zip(shard.card_masks, shard.cell_masks, masks, shard.gate_masks)))


@pytest.mark.parametrize("constraints", [None, {}, {"delay": {"minimum_ms": 0}}, {"delay": {"minimum_ms": 200}}])
def test_new_training_rejects_legacy_or_different_delay_contract(rollout, constraints):
    model, summary = rollout
    rewrite_constraints(summary, constraints)
    with pytest.raises(EventPreflightError, match="delay constraint"):
        load_complete_rollout([summary], expected_minimum_delay_ms=100)
    lanes, _ = load_complete_rollout([summary])
    assert preflight_event_rollout(model, lanes).passed


@pytest.mark.parametrize("corrupt", ["action", "mask"])
def test_contract_label_alone_does_not_make_old_actions_or_masks_valid(rollout, corrupt):
    _, summary = rollout
    rewrite_constraints(summary, delay_constraints_contract(), corrupt=corrupt)
    with pytest.raises(EventPreflightError, match="forbidden delay"):
        load_complete_rollout([summary], expected_minimum_delay_ms=100)


def test_new_contract_roundtrip_keeps_behavior_probabilities(rollout):
    model, summary = rollout
    rewrite_constraints(summary, delay_constraints_contract())
    lanes, counts = load_complete_rollout([summary], expected_minimum_delay_ms=100)
    assert counts["action_constraints"] == delay_constraints_contract()
    assert preflight_event_rollout(model, lanes).passed


@pytest.mark.parametrize("tick,act", [(200, True), (5998, False)])
def test_live_inference_uses_same_floor_and_returns_wait_at_deadline(monkeypatch, tick, act):
    from native_runner.live import policy as live_policy
    model = _event_model().eval()
    model.backbone.config = replace(model.backbone.config, snapshot_only=True)
    with torch.no_grad():
        model.gate_head[-1].bias.copy_(torch.tensor([-10000., 10000.]))
        model.delay_head[-1].bias[0] = 10000
    catalog = _catalog()
    deck = _deck()
    placement = dict(_placement(_grid([(1, 1)])), form_code=0, effective_cost=3.0)
    environment = _environment({"0": placement})
    observation = _observation([deck[0]])
    observation.tick, observation.episode_id = tick, "live-test"
    observation.metadata = {"missing": []}
    observation.action_mask = environment.action_mask(0)
    bundle = SimpleNamespace(card_catalog=catalog, ability_catalog=None, entity_archetype_catalog=None,
                             effect_catalog=None, card_specs=None)
    from native_runner.timeline import load_game_mode_timeline
    adapter = SimpleNamespace(bundle=bundle, observation=lambda row: observation,
                              tower_rows=lambda row: (), timeline=load_game_mode_timeline(72000006).timeline)
    tensorizer = SimpleNamespace(start_episode=lambda observation: None, tensorize=lambda observation: _batch(1))
    monkeypatch.setattr(live_policy, "load_event_checkpoint", lambda _: SimpleNamespace(model=model))
    monkeypatch.setattr(live_policy, "PartialLiveAdapter", lambda **kw: adapter)
    monkeypatch.setattr(live_policy, "UniversalObservationTensorizerV4", lambda **kw: tensorizer)
    monkeypatch.setattr(live_policy, "row_card_ids", lambda *a, **kw: torch.tensor(deck))
    row = dict(tick=tick, sequence=1, battle_epoch=1, own_side=0,
               own=dict(deck=[dict(card_id=card) for card in deck],
                        hand=[dict(slot=0, card=dict(card_id=deck[0], cost=3))], elixir_raw=60000),
               entities=[], public_events=dict(schema="cr-live-public-events.v1", started_tick=tick,
                   first_sequence=1, next_sequence=1, complete=True, events=[]))
    decision = live_policy.PartialLivePolicy("fixture").infer(row)
    assert decision["act"] == act
    assert decision["action_constraints"]["delay"] == {"minimum_ms": 100}
    if act:
        assert decision["delay_ms"] >= 100
