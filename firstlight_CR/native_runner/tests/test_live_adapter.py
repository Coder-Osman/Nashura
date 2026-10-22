import copy
import json
from pathlib import Path
import time

import pytest

from native_runner.live.adapter import PartialLiveAdapter
from native_runner.live.projection import TouchProjection
from native_runner.live.snapshot import LiveStateError, validate_snapshot, validate_touch
from native_runner.live.towers import public_tower_identity
from native_runner.match_factory import STANDARD_DECK
from native_runner.training.event_action_space import event_cell_to_native
from native_runner.training.v4.tensorizer import UniversalObservationTensorizerV4


@pytest.fixture
def live():
    deck = [{"deck_slot": i, "card_id": c, "form_code": 0, "cost": 3} for i, c in enumerate(STANDARD_DECK)]
    return {
        "schema": "cr-live-snapshot.v1", "event": "snapshot", "sequence": 1, "battle_epoch": 1,
        "tick": 200, "own_side": 0, "ended": False, "winner": None,
        "received_monotonic_ns": time.monotonic_ns(),
        "own": {"deck": deck, "hand": [{"slot": i, "card": deck[i]} for i in range(4)],
                "next_card": deck[4], "elixir_raw": 60000},
        "entities": [{"id": 5000010, "kind": 12, "owner": 1, "card_id": 26000000,
                      "native_data_id": 34000000, "x": 6000, "y": 20000,
                      "hp": 600, "max_hp": 600, "visibility": "visible"}],
    }


@pytest.fixture
def adapter():
    return PartialLiveAdapter()


def test_private_opponent_fields_remain_unknown(adapter, live):
    observation = adapter.observation(live)
    enemy = observation.players[1]
    assert enemy.elixir_exact is None and not enemy.hand and not enemy.deck and not enemy.cycle
    assert enemy.revealed_cards == (26000000,)
    assert observation.metadata["live_partial"]
    assert observation.entities[0].native_data_global_id == 34000000
    assert observation.players[0].metadata["hand_runtime_by_slot"]["0"]["form_code"] == 0


@pytest.mark.parametrize("kind", [12, 13, 15])
def test_unknown_visibility_rejected_for_every_enemy_kind(live, kind):
    live["entities"][0].update(kind=kind, visibility="unknown")
    with pytest.raises(LiveStateError, match="visible"):
        validate_snapshot(live)


def test_unknown_archetype_and_unsupported_forms_stop_inference(adapter, live):
    dummy = copy.deepcopy(live)
    dummy["entities"][0].update(native_data_id=4294967295, native_data_name="UnknownFixture")
    with pytest.raises(LiveStateError, match="unknown public archetype"):
        adapter.observation(dummy)
    live["own"]["deck"][0]["form_code"] = 2
    with pytest.raises(LiveStateError, match="unsupported live card form"):
        adapter.observation(live)


def test_evolution_form_requires_matching_static_definition(adapter, live):
    live['own']['deck'][0].update(card_id=26000057, form_code=1)
    with pytest.raises(LiveStateError, match=r'card=26000057.*form_code=1'):
        adapter.observation(live)


def test_evolved_building_placement_uses_separate_form_cache(adapter, live, monkeypatch):
    from native_runner.live import adapter as module
    live['own']['deck'][0].update(card_id=27000012, form_code=0)
    original = module.card_placement_mask
    observed = []

    def placement(*args, **kwargs):
        result = original(*args, **kwargs)
        if result.card_id == 27000012:
            observed.append(result.form)
        return result

    monkeypatch.setattr(module, 'card_placement_mask', placement)
    adapter.observation(live)
    live['own']['deck'][0]['form_code'] = 1
    evolved = adapter.observation(live)
    assert evolved.action_mask.placement_masks['0']['form_code'] == 1
    assert evolved.action_mask.placement_masks['0']['footprint_width_tiles'] == 3
    assert observed == ['base', 'evolution']
    live['own']['deck'][0]['form_code'] = 0
    base = adapter.observation(live)
    assert base.action_mask.placement_masks['0']['form_code'] == 0
    assert observed == ['base', 'evolution']


def test_source_building_does_not_misclassify_spawned_troop(adapter, live):
    live["entities"][0].update(card_id=27000001, native_data_id=34000019)
    assert adapter.observation(live).entities[0].entity_kind == "character"


def test_building_placement_excludes_visible_occupancy(adapter, live):
    live["own"]["deck"][0]["card_id"] = 27000009
    clear = adapter.observation(live).action_mask
    assert clear.hand_slots[0]
    catalog = adapter.bundle.entity_archetype_catalog
    tombstone = next(gid for gid, form in catalog.character_global_ids if form == "Tombstone")
    live["entities"].append({**live["entities"][0], "id": 5000011, "card_id": 27000009,
                             "native_data_id": tombstone, "owner": 0, "x": 6500, "y": 10500})
    occupied = adapter.observation(live).action_mask
    assert clear.placement_masks["0"]["row_major"][10][6]
    assert not occupied.placement_masks["0"]["row_major"][10][6]


def test_affordable_building_passes_full_tensorizer(adapter, live):
    live["own"]["deck"][0]["card_id"] = 27000009
    observation = adapter.observation(live)
    bundle = adapter.bundle
    deck = tuple(card["card_id"] for card in live["own"]["deck"])
    tensorizer = UniversalObservationTensorizerV4(
        actor_owner=0, card_catalog=bundle.card_catalog, ability_catalog=bundle.ability_catalog,
        entity_archetype_catalog=bundle.entity_archetype_catalog, effect_catalog=bundle.effect_catalog,
        card_specs=bundle.card_specs, deck=deck, deck_roles={c: (False, False) for c in deck},
        tracker=None, reject_unknown_public_semantics=True,
    )
    tensorizer.start_episode(observation)
    frame = tensorizer.tensorize(observation)
    assert frame.candidates.mask.sum() == 4
    assert observation.action_mask.placement_masks["0"]["footprint_width_tiles"] == 3
    assert observation.action_mask.placement_masks["0"]["footprint_height_tiles"] == 3


def test_unsigned_runtime_alias_keeps_its_exact_catalog_identity(adapter, live):
    live["entities"][0].update(native_data_id=3812815857, native_data_name="SpearGoblin_Dummy")
    assert adapter.observation(live).entities[0].native_data_global_id == 3812815857


@pytest.mark.parametrize("data_id,name,node", [
    (2573438603, "TombstoneHero_visual_dummy", "CHARACTER.TombstoneHero_visual_dummy"),
    (3587309677, "TombstoneHero_broken_visual_dummy", "EXT.TombstoneHero_broken_visual_dummy"),
])
def test_hero_helpers_keep_health_and_identity_but_are_not_spell_targets(adapter, live, data_id, name, node):
    from native_runner.competitive_data import load_competitive_data
    from native_runner.training.event_spell_constraints import (
        LightningTargetConstraintV1, LogTargetConstraintV1, lightning_target_allowed, log_target_allowed)
    tags = load_competitive_data("card_logic")["nodes"][node]["effective_record"]["GameTagsToSet"]
    assert {"NO_DAMAGE", "UNTARGETABLE"} <= set(tags.split(","))
    live["entities"][0].update(native_data_id=data_id, native_data_name=name,
                               card_id=203000088, hp=2559997, max_hp=2559997)
    observation = adapter.observation(live)
    helper, = observation.entities
    assert helper.native_data_global_id == data_id
    assert helper.hitpoints == helper.max_hitpoints == 2559997
    assert helper.visibility_state.targetable_by_owner == {"0": False, "1": False}
    assert helper.visibility_state.provenance.field_evidence["area_damage_eligible"].value == "static_declared"
    assert helper.runtime_provenance.field_evidence["visibility_state"].value == "static_declared"
    assert not lightning_target_allowed(observation, 0, 28000007, (6, 20), LightningTargetConstraintV1())
    assert not log_target_allowed(observation, 0, 28000011, (6, 19), LogTargetConstraintV1())


@pytest.mark.parametrize("data_id,name", [
    (990069281, "TombstoneHero_Monster_Active"), (1545925260, "TombstoneHeroSkeleton"),
    (3812815857, "SpearGoblin_Dummy"),
])
def test_real_summons_and_unrelated_dummy_names_remain_spell_targets(adapter, live, data_id, name):
    from native_runner.training.event_spell_constraints import (
        LightningTargetConstraintV1, LogTargetConstraintV1, lightning_target_allowed, log_target_allowed)
    live["entities"][0].update(native_data_id=data_id, native_data_name=name)
    observation = adapter.observation(live)
    assert observation.entities[0].visibility_state is None
    assert lightning_target_allowed(observation, 0, 28000007, (6, 20), LightningTargetConstraintV1())
    assert log_target_allowed(observation, 0, 28000011, (6, 19), LogTargetConstraintV1())


def test_helper_identity_requires_exact_data_id_and_name(adapter, live):
    live["entities"][0].update(native_data_id=2573438603, native_data_name="UnknownFixture")
    with pytest.raises(LiveStateError, match="identity mismatch"):
        adapter.observation(live)


def test_hero_tombstone_uses_root_footprint_without_casting_helpers_as_buildings(adapter, live):
    from native_runner.competitive_data import load_competitive_data
    definition = load_competitive_data("card_logic")["nodes"]["EXT.TombstoneHero"]
    assert definition["inheritance_root_node_id"] == "BUILDING.Tombstone"
    live["own"]["deck"][0]["card_id"] = 27000009
    clear = adapter.observation(live).action_mask.placement_masks["0"]["row_major"]
    # All three children share the hero card ID; only the building reserves tiles.
    for offset, data_id, name in ((0, 3368983219, "TombstoneHero"),
                                   (1, 2573438603, "TombstoneHero_visual_dummy"),
                                   (2, 990069281, "TombstoneHero_Monster_Active")):
        live["entities"].append(dict(live["entities"][0], id=5000020+offset,
            native_data_id=data_id, native_data_name=name, card_id=203000088,
            owner=0, x=6500, y=10500))
    observation = adapter.observation(live)
    assert clear[10][6]
    assert not observation.action_mask.placement_masks["0"]["row_major"][10][6]
    assert [e.entity_kind for e in observation.entities[-3:]] == ["building", "character", "character"]


@pytest.fixture
def chef_snapshot():
    # First eligible frame of the actual failed dashboard match (tick 153).
    return json.loads((Path(__file__).parent / "fixtures/live-chef-towers.json").read_text())


def test_captured_chef_towers_pass_full_tensorizer(adapter, chef_snapshot):
    observation = adapter.observation(chef_snapshot)
    by_id = {tower.entity_id: tower for tower in observation.towers}
    assert len(by_id) == 6 and not observation.entities
    assert by_id[5000000].tower_kind == "king"
    assert by_id[5000000].tower_troop_id == 159000004
    assert by_id[5000001].tower_kind == "princess_left"
    assert by_id[5000001].tower_troop_id == 159000000
    assert by_id[5000003].tower_troop_id is None
    for entity in chef_snapshot["entities"]:
        tower = by_id[entity["id"]]
        assert (tower.owner, tower.position, tower.hitpoints, tower.max_hitpoints) == (
            entity["owner"], (entity["x"], entity["y"]), entity["hp"], entity["max_hp"])
        assert tower.tower_troop_runtime is None
    bundle = adapter.bundle
    deck = tuple(card["card_id"] for card in chef_snapshot["own"]["deck"])
    tensorizer = UniversalObservationTensorizerV4(
        actor_owner=chef_snapshot["own_side"], card_catalog=bundle.card_catalog,
        ability_catalog=bundle.ability_catalog, entity_archetype_catalog=bundle.entity_archetype_catalog,
        effect_catalog=bundle.effect_catalog, card_specs=bundle.card_specs, deck=deck,
        deck_roles={c: (False, False) for c in deck}, tracker=None,
        reject_unknown_public_semantics=True,
    )
    tensorizer.start_episode(observation)
    frame = tensorizer.tensorize(observation)
    assert frame.towers.mask.sum() == 6
    assert (frame.towers.tower_troop_type == 4).sum() == 1


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("x,kind", [(3500, "princess_left"), (14500, "princess_right")])
def test_chef_tower_owner_and_lane_are_preserved(adapter, chef_snapshot, owner, x, kind):
    chef_snapshot["entities"] = [dict(chef_snapshot["entities"][1], owner=owner, x=x)]
    tower = adapter.observation(chef_snapshot).towers[0]
    assert tower.owner == owner and tower.tower_kind == kind


@pytest.mark.parametrize("change", [
    {"native_data_id": 123456, "native_data_name": "ChefTowerKing"},
    {"native_data_name": "UnknownTower"}, {"kind": 15},
])
def test_chef_identity_is_not_guessed_from_name(adapter, chef_snapshot, change):
    chef_snapshot["entities"][0].update(change)
    with pytest.raises(LiveStateError):
        adapter.observation(chef_snapshot)


def test_crown_towers_are_not_public_spawn_events(chef_snapshot, live):
    entities = chef_snapshot["entities"] + live["entities"]
    assert {e["id"] for e in entities if public_tower_identity(e) is None} == {5000010}


def test_touch_requires_current_elixir(live):
    live["own"]["elixir_raw"] = 10000
    with pytest.raises(LiveStateError, match="affordable"):
        validate_touch(live, epoch=1, slot=0, card_id=26000000)


@pytest.mark.parametrize("tick,remaining,multiplier,phase", [
    (200, 290000, 1, "normal"), (2400, 180000, 2, "normal"),
    (3600, 120000, 2, "overtime"), (4800, 60000, 3, "overtime"),
    (6000, 0, 3, "tiebreak"),
])
def test_standard_ladder_clock_matches_training(adapter, live, tick, remaining, multiplier, phase):
    live["tick"] = tick
    observation = adapter.observation(live)
    assert observation.time.remaining_ms == remaining
    assert observation.time.elixir_multiplier == multiplier
    assert observation.phase == phase
    if tick == 6000:
        assert not any(observation.action_mask.hand_slots)


def calibration_pairs():
    return [{"native": [x, y], "owner": 1,
             "screen": [60 + .053 * x, 1450 - .039 * (32000 - y)]}
            for x in (2500, 15500) for y in (19000, 30500)]


def test_camera_transform_agrees_for_both_native_owners():
    projection = TouchProjection(calibration_pairs())
    for model_cell in (0, 25, 180, 310, 575):
        cells = [event_cell_to_native(model_cell, owner=o, board_width=18) for o in (0, 1)]
        assert projection.screen(cells[0], 0) == projection.screen(cells[1], 1)
    assert projection.screen((0, 0), 0)[0] > projection.screen((17, 0), 0)[0]
    assert projection.screen((0, 0), 0)[1] > projection.screen((0, 31), 0)[1]
    assert (projection.screen((5, 5), 0, model_subcell_offset=(0.5, 0.5))
            == projection.screen((12, 26), 1, model_subcell_offset=(0.5, 0.5)))


def test_calibration_rejects_narrow_or_inconsistent_samples():
    with pytest.raises(LiveStateError):
        TouchProjection(calibration_pairs()[:3])
    pairs = calibration_pairs()
    pairs.append({"native": [2500, 19000], "owner": 1, "screen": [950, 300]})
    with pytest.raises(LiveStateError, match="residual"):
        TouchProjection(pairs)
