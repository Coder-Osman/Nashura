"""Lightning geometry, opt-in compatibility and PPO probability consistency."""

import argparse
from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import EntityStateV1, TowerStateV1
from native_runner.tests.test_event_action_space import _environment, _frame, _grid, _placement
from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.event_action_space import event_cell_to_native, legal_event_actions
from native_runner.training.event_cache import load_event_rl_shard, write_event_rl_shard
from native_runner.training.event_preflight import EventPreflightError, load_complete_rollout, preflight_event_rollout
from native_runner.training.event_rollout import EventLaneRecorderV1
from native_runner.training.event_spell_constraints import (
    LIGHTNING_CARD_ID, LOG_CARD_ID, LightningTargetConstraintV1, LogTargetConstraintV1, action_constraints_contract,
    add_lightning_target_arguments,
    lightning_constraint_from_args, lightning_target_allowed, spell_target_allowed,
)
from native_runner.training.v4.catalog import CardCatalogV1
from tools.demo_event_selfplay import legal_play_options

DECK = (LIGHTNING_CARD_ID, 28000011, 26000009, 26000048, 26000043, 26000015, 26000039, 27000009)
CATALOG = CardCatalogV1.from_raw_ids(DECK)
ALL_CELLS = [(x, y) for y in range(32) for x in range(18)]


def _enemy(**kwargs):
    return EntityStateV1(**{
        "entity_id": 42, "owner": 1, "card_id": 26000009, "entity_kind": "troop",
        "position": (5500.0, 9500.0), "hitpoints": 1000.0, **kwargs,
    })


def _scene(*, owner=0, entities=(), towers=(), hand=(LIGHTNING_CARD_ID,)):
    return SimpleNamespace(
        entities=tuple(entities), towers=tuple(towers), tick=1,
        players=[SimpleNamespace(owner=owner, hand=list(hand), elixir_exact=10.0)],
        time=SimpleNamespace(elixir_multiplier=1.0),
    )


def _legal(observation, constraint, *, owner=0, cells=ALL_CELLS, hand_slots=(True,) * 4, log_constraint=None):
    env = _environment({str(i): _placement(_grid(cells)) for i in range(len(observation.players[0].hand))},
                       hand_slots=hand_slots, costs=[6.0] * len(observation.players[0].hand))
    result = legal_event_actions(
        env, observation, _frame(CATALOG, list(DECK)), owner, catalog=CATALOG,
        card_count=8, cell_count=576, board_width=18, board_height=32,
        delay_offset_ms=(0, 100, 6000), lightning_constraint=constraint,
        log_constraint=log_constraint,
    )
    return env, result


def test_resource_radius_and_configurable_inclusive_margin():
    constraint = LightningTargetConstraintV1()
    assert constraint.spell_radius_tiles == 3.5
    assert constraint.radius_tiles == 4.0
    observation = _scene(entities=[_enemy()])
    assert lightning_target_allowed(observation, 0, LIGHTNING_CARD_ID, (5, 5), constraint)
    assert not lightning_target_allowed(observation, 0, LIGHTNING_CARD_ID, (5, 5),
                                        LightningTargetConstraintV1(margin_tiles=0))
    observation.entities = (_enemy(position=(5500, 9500.001)),)
    assert not lightning_target_allowed(observation, 0, LIGHTNING_CARD_ID, (5, 5), constraint)
    observation.entities = (_enemy(position=(5500, 9000)),)
    assert lightning_target_allowed(observation, 0, LIGHTNING_CARD_ID, (5, 5),
                                    LightningTargetConstraintV1(margin_tiles=0))


@pytest.mark.parametrize("changes", [
    {"owner": 0}, {"owner": None}, {"hitpoints": 0}, {"hitpoints": None},
    {"visible": False}, {"entity_kind": "projectile"}, {"entity_kind": "effect"},
    {"position": (float("nan"), 9500)},
])
def test_non_targets_do_not_enable_lightning(changes):
    _, legal = _legal(_scene(entities=[_enemy(**changes)]), LightningTargetConstraintV1())
    assert not legal.act_allowed
    assert not legal.playable_now
    assert not legal.card_mask.any() and not legal.cell_masks.any() and not legal.delay_masks.any()


def test_area_eligibility_not_single_target_visibility():
    target = SimpleNamespace(
        owner=1, hitpoints=1000, entity_kind="troop", position=(5500, 9500),
        projectile_state=None, visible=True,
    )
    target.visibility_state = SimpleNamespace(public_by_owner={"0": True},
                                             targetable_by_owner={"0": False}, area_damage_eligible=True)
    observation = _scene(entities=[target])
    constraint = LightningTargetConstraintV1()
    assert lightning_target_allowed(observation, 0, LIGHTNING_CARD_ID, (5, 5), constraint)
    target.visibility_state.area_damage_eligible = False
    assert not lightning_target_allowed(observation, 0, LIGHTNING_CARD_ID, (5, 5), constraint)
    target.visibility_state.area_damage_eligible = True
    target.visibility_state.public_by_owner["0"] = False
    assert not lightning_target_allowed(observation, 0, LIGHTNING_CARD_ID, (5, 5), constraint)


@pytest.mark.parametrize("kind", ["troop", "building", "spell"])
def test_live_entities_include_air_ground_and_spell_spawned_units(kind):
    assert _legal(_scene(entities=[_enemy(entity_kind=kind)]), LightningTargetConstraintV1())[1].act_allowed


def test_tower_only_finisher_includes_unactivated_king_but_not_destroyed_tower():
    tower = TowerStateV1(entity_id=99, owner=1, tower_kind="king", position=(5500, 9500),
                         hitpoints=1000, max_hitpoints=1000, active=False)
    _, legal = _legal(_scene(towers=[tower]), LightningTargetConstraintV1())
    assert legal.act_allowed and legal.cell_masks[0, 5 * 18 + 5]
    assert not _legal(_scene(towers=[replace(tower, hitpoints=0)]), LightningTargetConstraintV1())[1].act_allowed


def test_default_off_and_other_cards_preserve_native_legality():
    _, disabled = _legal(_scene(), None)
    assert disabled.cell_masks[0].sum() == 576
    _, constrained = _legal(_scene(hand=DECK[:2]), LightningTargetConstraintV1())
    assert not constrained.card_mask[0]
    assert constrained.card_mask[1] and constrained.cell_masks[1].sum() == 576
    assert constrained.act_allowed and constrained.playable_now
    assert lightning_target_allowed(_scene(), 0, DECK[1], (5, 5), LightningTargetConstraintV1())


def test_intersection_preserves_engine_mask_and_elixir_delay():
    constraint = LightningTargetConstraintV1()
    observation = _scene(entities=[_enemy()])
    cells = [(5, 5), (0, 0)]
    env, legal = _legal(observation, constraint, cells=cells)
    assert legal.cell_masks[0].nonzero().flatten().tolist() == [5 * 18 + 5]
    assert env.action_mask(0).placement_masks["0"]["row_major"] == tuple(tuple(row) for row in _grid(cells))
    observation.players[0].elixir_exact = 5.0
    _, delayed = _legal(observation, constraint, cells=cells, hand_slots=(False,) * 4)
    assert delayed.act_allowed and not delayed.playable_now
    assert delayed.delay_masks[0].tolist() == [False, False, True]


def test_owner_rotation_and_demo_use_identical_target_geometry():
    constraint = LightningTargetConstraintV1()
    observations = [_scene(entities=[_enemy()]), _scene(owner=1, entities=[_enemy(owner=0, position=(12500, 22500))])]
    results = [_legal(obs, constraint, owner=owner) for owner, obs in enumerate(observations)]
    assert torch.equal(results[0][1].cell_masks, results[1][1].cell_masks)
    for owner, (env, legal) in enumerate(results):
        options, _ = legal_play_options(env, observations[owner], owner, lightning_constraint=constraint)
        assert {y * 18 + x for x, y in options[LIGHTNING_CARD_ID][1]} == set(legal.cell_masks[0].nonzero().flatten().tolist())
        for index in legal.cell_masks[0].nonzero().flatten().tolist():
            assert lightning_target_allowed(observations[owner], owner, LIGHTNING_CARD_ID,
                                            event_cell_to_native(index, owner=owner, board_width=18), constraint)


@pytest.mark.parametrize("margin", [-0.1, float("inf"), float("nan")])
def test_invalid_margins_rejected(margin):
    with pytest.raises(ValueError, match="finite and non-negative"):
        LightningTargetConstraintV1(margin_tiles=margin)


def test_cli_explicit_opt_in():
    parser = argparse.ArgumentParser()
    add_lightning_target_arguments(parser)
    assert lightning_constraint_from_args(parser.parse_args([])) is None
    assert lightning_constraint_from_args(parser.parse_args(["--lightning-target-constraint"])).radius_tiles == 4
    with pytest.raises(ValueError, match="requires"):
        lightning_constraint_from_args(parser.parse_args(["--lightning-target-margin-tiles", "1"]))


@pytest.mark.parametrize("spells", ["lightning", "log", "both"])
def test_sampled_constraint_masks_roundtrip_and_rescore_with_no_target_wait(tmp_path, spells):
    torch.manual_seed(8)
    model = _event_model().eval()
    constraint = LightningTargetConstraintV1() if spells != "log" else None
    log_constraint = LogTargetConstraintV1() if spells != "lightning" else None
    hand = (LIGHTNING_CARD_ID,) if spells == "lightning" else (LOG_CARD_ID,) if spells == "log" else DECK[:2]
    metadata = action_constraints_contract(constraint, log_constraint)
    recorder = EventLaneRecorderV1(match_id="lightning", policy_version="test", temperatures=(1.,) * 4,
                                   decks={0: DECK, 1: DECK}, action_constraints=metadata)
    saved_masks = []
    with torch.inference_mode():
        for i in range(3):
            observation = _scene(entities=[_enemy(native_data_global_id=34_000_010)] if i != 1 else [], hand=hand)
            env = _environment({str(slot): _placement(_grid(ALL_CELLS)) for slot in range(len(hand))})
            legal = legal_event_actions(
                env, observation, _frame(CATALOG, list(DECK)), 0, catalog=CATALOG,
                card_count=8, cell_count=576, board_width=18, board_height=32,
                delay_offset_ms=model.event_config.delay_offset_ms, lightning_constraint=constraint,
                log_constraint=log_constraint,
            )
            masks = dict(card_mask=legal.card_mask[None], cell_masks=legal.cell_masks[None],
                         delay_mask=legal.delay_masks[None], gate_mask=torch.tensor([[not legal.act_allowed, legal.act_allowed]]))
            frame = _batch(1)
            context = model.encode(frame.to_model_input("cpu", float_dtype=torch.float32), model.initial_state(1),
                                   episode_start=torch.ones(1, dtype=torch.bool))
            step = model.select_action(context, **masks)
            assert bool(step.action.act[0]) == (i != 1)
            if i != 1:
                selected = DECK[int(step.action.card_row[0])]
                assert selected in hand
                assert spell_target_allowed(
                    observation, 0, selected,
                    event_cell_to_native(int(step.action.cell[0]), owner=0, board_width=18),
                    lightning_constraint=constraint, log_constraint=log_constraint,
                )
            recorder.decide(owner=0, tick=100 + i * 10, frame=frame, step=step, **masks)
            if bool(step.action.act[0]):
                from native_runner.tests.test_event_rollout import _confirm
                _confirm(recorder, 0, 100 + i * 10)
            recorder.advance(ticks=10, done=i == 2, rewards={0: 1.} if i == 2 else {})
            saved_masks.append(legal.cell_masks)
    path = tmp_path / "part-000.crev.zst"
    recorder.to_shard(path, shard_id="constraint", contract={})
    shard = load_event_rl_shard(tmp_path / "part-000.json", verify=True)
    assert shard.manifest["contract"]["action_constraints"] == metadata
    assert torch.equal(shard.cell_masks, torch.stack(saved_masks))
    lanes, counts = load_complete_rollout([tmp_path / "part-000.json"])
    assert counts["action_constraints"] == metadata
    assert preflight_event_rollout(model, lanes).passed
    with pytest.raises(ValueError, match="action_constraints"):
        recorder.to_shard(tmp_path / "bad.crev.zst", shard_id="bad", contract={"action_constraints": None})
    # Same checkpoint and recorded probabilities, but incompatible policy rules.
    for other in (None, action_constraints_contract(
        LightningTargetConstraintV1(margin_tiles=1) if constraint else None,
        LogTargetConstraintV1(margin_tiles=1) if log_constraint else None,
    )):
        write_event_rl_shard(
            tmp_path / "part-001.crev.zst", shard.records, list(shard.observations()), shard_id="other",
            contract={"action_constraints": other},
            masks=list(zip(shard.card_masks, shard.cell_masks, shard.delay_masks, shard.gate_masks)),
        )
        with pytest.raises(EventPreflightError, match="action_constraints mismatch"):
            load_complete_rollout([tmp_path / "part-000.json", tmp_path / "part-001.json"])
