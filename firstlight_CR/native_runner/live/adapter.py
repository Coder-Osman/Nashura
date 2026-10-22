"""Explicitly partial live observations for experimental policy diagnostics.

This adapter does not satisfy the rich offline telemetry contract. Unavailable
runtime fields remain unknown; they must not be used for training/evaluation.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import copy

from ..arena import OccupiedFootprintV1, card_placement_mask, native_building_footprint
from ..contracts import (ActionMaskV1, EntityStateV1, ObservationTier, ObservationV1,
                         PlayerStateV1, TimeStateV1, TowerStateV1,
                         SemanticProvenanceV1, PLAYER_RUNTIME_SEMANTIC_FIELDS)
from ..training.v4.factory import production_semantic_bundle
from ..training.v4.catalog import UNKNOWN_ENTITY_ARCHETYPE_VOCAB_ID
from ..training.card_features import ENTITY_FORM_TO_BASE_CARD
from ..timeline import load_game_mode_timeline
from .snapshot import LiveStateError, model_blockers, validate_snapshot
from .towers import public_tower_identity
from .runtime import entity_runtime, runtime_provenance
from .cards import placement_form, ready_evolutions


class PartialLiveAdapter:
    def __init__(self):
        self.bundle = production_semantic_bundle()
        self.timeline = load_game_mode_timeline(72000006).timeline
        self.placement_cache = {}
        self.tower_cache = {}
        self.identity = None
        self.last_tick = -1

    def fork(self):
        clone = copy.copy(self)
        clone.tower_cache = dict(self.tower_cache)
        clone.placement_cache = dict(self.placement_cache)
        return clone

    def tower_rows(self, row):
        identity = (row["battle_epoch"], row["own_side"], tuple(c["card_id"] for c in row["own"]["deck"]))
        if identity != self.identity or row["tick"] < self.last_tick:
            self.tower_cache = {}
            self.identity = identity
        self.last_tick = row["tick"]
        current = {e["id"]: e for e in row["entities"] if public_tower_identity(e) is not None}
        complete = row.get("coverage", {}).get("base_entities") is True
        if complete and (len(current) == 6 or len(self.tower_cache) == 6):
            self.tower_cache.update(current)
            return tuple(current.get(key, {**value, "hp": 0, "phase": None})
                         for key, value in self.tower_cache.items())
        return tuple(current.values())

    def observation(self, row: dict) -> ObservationV1:
        validate_snapshot(row)
        owner, own = row["own_side"], row["own"]
        phase, multiplier = self.timeline.phase(row["tick"])
        deck = tuple(c["card_id"] for c in own["deck"])
        evolutions = ready_evolutions(row, self.bundle.card_specs)
        towers, entities = [], []
        tower_rows = self.tower_rows(row)
        current_entities = [e for e in row["entities"] if public_tower_identity(e) is None]
        for e in [*tower_rows, *current_entities]:
            data_id = e.get("native_data_id")
            tower = public_tower_identity(e)
            runtime = entity_runtime(row, e)
            if tower is not None:
                kind, tower_troop = tower
                towers.append(TowerStateV1(
                    entity_id=e["id"], owner=e["owner"], tower_kind=kind,
                    position=(e["x"], e["y"]), hitpoints=e["hp"], max_hitpoints=e["max_hp"],
                    tower_troop_id=tower_troop,
                    active=e["hp"] > 0,
                    visible_target=runtime.get("visible_target"), attack_state=runtime.get("attack_state"),
                    runtime_provenance=runtime_provenance(row, runtime, tower=True),
                ))
                continue
            if data_id is None:
                raise LiveStateError("entity has no native archetype identity")
            card_id = e["card_id"] if e["card_id"] > 0 else None
            vocab_id = self.bundle.entity_archetype_catalog.runtime_global_vocab_id(data_id)
            if vocab_id == UNKNOWN_ENTITY_ARCHETYPE_VOCAB_ID:
                raise LiveStateError(f"unknown public archetype: {data_id} ({e.get('native_data_name', 'unnamed')})")
            archetype = self.bundle.entity_archetype_catalog.metadata_for_vocab_id(vocab_id)
            entities.append(EntityStateV1(
                entity_id=e["id"], owner=e["owner"], card_id=card_id,
                entity_kind=archetype.child_kind,
                native_data_global_id=data_id, position=(e["x"], e["y"]),
                hitpoints=e["hp"], max_hitpoints=e["max_hp"],
                visible=e["visibility"] == "visible",
                runtime_provenance=runtime_provenance(row, runtime),
                **runtime,
            ))
        hand = tuple(s["card"]["card_id"] for s in own["hand"] if s["card"])
        slot_map = {str(s["card"]["card_id"]): s["slot"] for s in own["hand"] if s["card"]}
        crowns = {side: 0 for side in (0, 1)}
        if len(towers) == 6:
            for tower in towers:
                if not tower.active:
                    crowns[1-tower.owner] = (3 if tower.tower_kind == "king"
                                             else min(3, crowns[1-tower.owner]+1))
        actors = {
            owner: PlayerStateV1(
                owner=owner, crowns=crowns[owner], private_state_visible=True, deck=deck, hand=hand,
                next_card=own["next_card"]["card_id"] if own["next_card"] else None,
                elixir_exact=own["elixir_raw"] / 10000,
                evolution_runtime_states=evolutions,
                runtime_provenance=SemanticProvenanceV1(
                    field_evidence={key: "native_derived" if key == "evolution_runtime_states" and evolutions
                                    else "unknown" for key in PLAYER_RUNTIME_SEMANTIC_FIELDS},
                    source_fields={"evolution_runtime_states": ("own.deck native selection form_code=1",)}
                                  if evolutions else {}, observed_tick=row["tick"],
                    notes=("Only currently ready own evolutions are projected; other cycle states stay unknown",)),
                metadata={"hand_slot_by_card": slot_map, "native_hand_capacity": 4,
                          "hand_runtime_by_slot": {str(s["slot"]): s["card"]
                                                   for s in own["hand"] if s["card"]}},
            ),
            1 - owner: PlayerStateV1(owner=1 - owner, crowns=crowns[1-owner], revealed_cards=tuple(sorted({
                e.card_id for e in entities if e.owner != owner and e.card_id in self.bundle.card_specs
            }))),
        }
        observation = ObservationV1(
            tier=ObservationTier.FAIR, tick=row["tick"], owner=owner,
            episode_id=f"live:{row['battle_epoch']}:{owner}",
            time=TimeStateV1(elapsed_ms=row["tick"] * 50,
                             remaining_ms=max(0, self.timeline.maximum_tick - row["tick"]) * 50,
                             elixir_multiplier=multiplier),
            phase=phase,
            players=tuple(actors[i] for i in (0, 1)), towers=tuple(towers), entities=tuple(entities),
            metadata={"live_partial": True, "missing": model_blockers(row)},
        )
        return replace(observation, action_mask=self.action_mask(row))

    def action_mask(self, row: dict) -> ActionMaskV1:
        owner, own = row["own_side"], row["own"]
        placements, slots = {}, [False] * 4
        towers = self.tower_rows(row)
        known_towers = len(towers) == 6
        destroyed = tuple(sorted(public_tower_identity(e)[0].removeprefix("princess_")
                                 for e in towers if e["owner"] != owner and e["hp"] == 0
                                 and public_tower_identity(e)[0].startswith("princess_")))
        active_towers = tuple(OccupiedFootprintV1(e["x"], e["y"],
                                4 if public_tower_identity(e)[0] == "king" else 3,
                                4 if public_tower_identity(e)[0] == "king" else 3, f"live-tower:{e['id']}")
                              for e in towers if e["hp"] > 0) if known_towers else None
        occupied = []
        for entity in row["entities"]:
            if public_tower_identity(entity) is not None:
                continue  # Crown tower footprints are part of the static mask.
            vocab = self.bundle.entity_archetype_catalog.runtime_global_vocab_id(entity.get("native_data_id", 0))
            archetype = self.bundle.entity_archetype_catalog.metadata_for_vocab_id(vocab)
            if archetype.child_kind != "building":
                continue
            # Public hero/evolution spell IDs use the same canonical bridge
            # as the model tensorizer. Only exact building archetypes reach
            # this path; visual helpers and spawned troops keep their types.
            card_id = ENTITY_FORM_TO_BASE_CARD.get(entity["card_id"], entity["card_id"])
            spec = self.bundle.card_specs.get(card_id)
            footprint = native_building_footprint(spec) if spec else None
            if footprint is None:
                raise LiveStateError("visible building lacks a verified occupancy footprint: "
                                     f"{entity['native_data_id']} ({entity.get('native_data_name', 'unnamed')})")
            occupied.append(OccupiedFootprintV1(entity["x"], entity["y"], footprint.width_tiles,
                                                 footprint.height_tiles, f"live:{entity['id']}"))
        for entry in own["hand"]:
            card, slot = entry["card"], entry["slot"]
            if not card:
                continue
            spec = self.bundle.card_specs.get(card["card_id"])
            if spec is None:
                raise LiveStateError("unknown actor card")
            form = placement_form(card, self.bundle.card_specs)
            key = (owner, card["card_id"], form,
                   tuple((e["id"], e["hp"] > 0) for e in towers) if known_towers else ())
            if key not in self.placement_cache:
                self.placement_cache[key] = card_placement_mask(
                    spec, owner, form=form, ruleset_id=hashlib.sha256(b"live-partial-standard-1v1").hexdigest(),
                    destroyed_enemy_princess_lanes=destroyed, active_tower_footprints=active_towers)
            mask = self.placement_cache[key]
            if spec.kind.value == "building" and occupied:
                mask = card_placement_mask(spec, owner, form=form, ruleset_id=mask.ruleset_id,
                                           occupied_building_footprints=tuple(occupied),
                                           destroyed_enemy_princess_lanes=destroyed,
                                           active_tower_footprints=active_towers)
            placements[str(slot)] = {"row_major": mask.rows, "effective_cost": card["cost"],
                                     "form_code": card["form_code"], "source_native_hand_slot": slot,
                                     "model_subcell_offset": mask.model_subcell_offset,
                                     "footprint_width_tiles": mask.footprint_width_tiles,
                                     "footprint_height_tiles": mask.footprint_height_tiles}
            slots[slot] = (own["elixir_raw"] >= card["cost"] * 10000 and not row["ended"]
                           and row["tick"] < self.timeline.maximum_tick)
        return ActionMaskV1(
            kinds={"wait": True, "play_card": any(slots)}, hand_slots=tuple(slots),
            placement_masks=placements,
            reasons={"effective_elixir": own["elixir_raw"] / 10000, "decision_ready": True,
                     "source": "conservative_static_placement"},
        )
