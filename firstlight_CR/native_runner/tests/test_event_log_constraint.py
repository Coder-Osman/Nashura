"""The Log checks a forward ground sweep, including exact child air identity."""

import argparse
from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import TowerStateV1
from native_runner.tests.test_event_spell_constraints import _enemy, _scene, _legal
from native_runner.training.event_spell_constraints import (
    LOG_CARD_ID, LogTargetConstraintV1, add_lightning_target_arguments, add_log_target_arguments,
    action_constraints_contract, lightning_constraint_from_args, log_constraint_from_args, log_target_allowed,
)
from tools.demo_event_selfplay import legal_play_options


def _ground(**kwargs):
    return _enemy(**{"native_data_global_id": 34_000_010, **kwargs})


def test_geometry_comes_from_rolling_projectile_not_initial_drop():
    constraint = LogTargetConstraintV1()
    assert (constraint.travel_tiles, constraint.half_width_tiles, constraint.half_depth_tiles) == (10.1, 1.95, 0.6)
    assert constraint.margin_tiles == 0.5


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("dx,forward,allowed", [
    (0, 0, True), (0, 8000, True), (2450, 5000, True), (2450.01, 5000, False),
    (-2450, 5000, True), (-2450.01, 5000, False),
    (0, -1100, True), (0, -1100.01, False), (0, 11200, True), (0, 11200.01, False),
    (3000, 1000, False), (0, -4000, False),
])
def test_forward_sweep_boundaries_and_owner_direction(owner, dx, forward, allowed):
    # Same cast point, opposite native direction for owner 1.
    target = _ground(owner=1 - owner, position=(8500 + dx, 15500 + (1 if owner == 0 else -1) * forward))
    observation = _scene(owner=owner, entities=[target], hand=(LOG_CARD_ID,))
    assert log_target_allowed(observation, owner, LOG_CARD_ID, (8, 15), LogTargetConstraintV1()) is allowed


def test_margin_is_configurable():
    observation = _scene(entities=[_ground(position=(7500, 10500))], hand=(LOG_CARD_ID,))
    assert log_target_allowed(observation, 0, LOG_CARD_ID, (5, 5), LogTargetConstraintV1())
    assert not log_target_allowed(observation, 0, LOG_CARD_ID, (5, 5), LogTargetConstraintV1(margin_tiles=0))


@pytest.mark.parametrize("global_id,parent_card,kind,allowed", [
    (34_000_010, 26000009, "troop", True),       # Golem
    (34_000_015, 26000015, "troop", False),      # Baby Dragon
    (34_000_037, 26000039, "troop", False),      # Mega Minion
    (34_000_050, 26000048, "troop", False),      # Night Witch's Bat (parent is ground)
    (34_000_008, 28000010, "spell", True),       # Spell-spawned Skeleton
    (35_000_011, 27000009, "building", True),    # Tombstone
    (34_000_008, 26000015, "troop", True),       # Parent air identity does not override child
    (None, 26000009, "troop", False),           # Unknown is not proven ground
    (4_000_000_000, 26000009, "troop", False),
])
def test_actual_entity_identity_filters_air_and_unknown(global_id, parent_card, kind, allowed):
    target = _ground(native_data_global_id=global_id, card_id=parent_card, entity_kind=kind)
    observation = _scene(entities=[target], hand=(LOG_CARD_ID,))
    _, legal = _legal(observation, None, log_constraint=LogTargetConstraintV1())
    assert legal.act_allowed is allowed
    assert bool(legal.card_mask[1]) is allowed


@pytest.mark.parametrize("global_id,form,allowed", [
    (None, "Golem", True), (None, "BabyDragon", False),
    (34_000_015, "Golem", False), (4_000_000_000, "Golem", False),
])
def test_current_form_fallback_matches_tensorizer_identity_rules(global_id, form, allowed):
    target = SimpleNamespace(
        owner=1, hitpoints=1000, position=(5500, 9500), entity_kind="troop", visibility_state=None,
        native_data_global_id=global_id, evolution_state=SimpleNamespace(current_form_id=form),
    )
    assert log_target_allowed(_scene(entities=[target]), 0, LOG_CARD_ID, (5, 5), LogTargetConstraintV1()) is allowed


@pytest.mark.parametrize("changes", [{"owner": 0}, {"owner": None}, {"hitpoints": 0}, {"hitpoints": None},
                                     {"visible": False}, {"entity_kind": "projectile"}, {"entity_kind": "effect"}])
def test_friendly_dead_and_nonunit_objects_do_not_enable_log(changes):
    observation = _scene(entities=[_ground(**changes)], hand=(LOG_CARD_ID,))
    assert not _legal(observation, None, log_constraint=LogTargetConstraintV1())[1].act_allowed


def test_towers_are_ground_targets_and_destroyed_towers_are_excluded():
    tower = TowerStateV1(entity_id=99, owner=1, tower_kind="king", position=(5500, 15500),
                         hitpoints=1000, max_hitpoints=1000, active=False)
    observation = _scene(towers=[tower], hand=(LOG_CARD_ID,))
    assert log_target_allowed(observation, 0, LOG_CARD_ID, (5, 5), LogTargetConstraintV1())
    observation.towers = (replace(tower, hitpoints=0),)
    assert not _legal(observation, None, log_constraint=LogTargetConstraintV1())[1].act_allowed


def test_off_preserves_mask_and_no_ground_target_masks_card_and_act():
    observation = _scene(hand=(LOG_CARD_ID,))
    assert _legal(observation, None)[1].cell_masks[1].sum() == 576
    _, legal = _legal(observation, None, log_constraint=LogTargetConstraintV1())
    assert not legal.act_allowed and not legal.playable_now
    assert not legal.card_mask.any() and not legal.cell_masks.any() and not legal.delay_masks.any()


def test_native_intersection_rotation_and_demo_share_sweep():
    constraint = LogTargetConstraintV1()
    cells = [(5, 5), (5, 15), (0, 5)]
    observations = [_scene(entities=[_ground()], hand=(LOG_CARD_ID,)),
                    _scene(owner=1, entities=[_ground(owner=0, position=(12500, 22500))], hand=(LOG_CARD_ID,))]
    results = []
    for owner, observation in enumerate(observations):
        native_cells = cells if owner == 0 else [(17 - x, 31 - y) for x, y in cells]
        env, legal = _legal(observation, None, owner=owner, cells=native_cells, log_constraint=constraint)
        assert legal.cell_masks[1].nonzero().flatten().tolist() == [5 * 18 + 5]
        assert sum(sum(row) for row in env.action_mask(owner).placement_masks["0"]["row_major"]) == 3
        options, _ = legal_play_options(env, observation, owner, log_constraint=constraint)
        assert options[LOG_CARD_ID][1] == [(5, 5)]
        results.append(legal)
    assert torch.equal(results[0].cell_masks, results[1].cell_masks)


@pytest.mark.parametrize("margin", [-1, float("inf"), float("nan")])
def test_invalid_margin_rejected(margin):
    with pytest.raises(ValueError, match="finite and non-negative"):
        LogTargetConstraintV1(margin_tiles=margin)


def test_cli_switches_are_independent_and_contract_keeps_existing_lightning_identity():
    parser = argparse.ArgumentParser()
    add_lightning_target_arguments(parser)
    add_log_target_arguments(parser)
    default = parser.parse_args([])
    assert log_constraint_from_args(default) is None and lightning_constraint_from_args(default) is None
    only_log = parser.parse_args(["--log-target-constraint"])
    log = log_constraint_from_args(only_log)
    assert lightning_constraint_from_args(only_log) is None
    assert action_constraints_contract(None, log) == {"log": log.to_dict(), "delay": {"minimum_ms": 100}}
    both = parser.parse_args(["--log-target-constraint", "--lightning-target-constraint"])
    lightning = lightning_constraint_from_args(both)
    assert action_constraints_contract(lightning, log)["lightning"] == action_constraints_contract(lightning)["lightning"]
    with pytest.raises(ValueError, match="requires"):
        log_constraint_from_args(parser.parse_args(["--log-target-margin-tiles", "1"]))
