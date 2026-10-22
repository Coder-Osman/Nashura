"""Stateless event checkpoint inference from explicitly partial live input."""
from __future__ import annotations

import time
import secrets

import torch

from ..match_factory import NATIVE_GAMEPLAY_END_TICK
from ..training.event_action_space import (event_cell_to_native, event_placement_rows_to_model,
                                           row_card_ids, minimum_playable_delay_ms)
from ..training.event_cache import canonicalize_event_observation
from ..training.event_checkpoint import load_event_checkpoint
from ..training.event_delay_constraints import playable_delay_mask
from ..training.event_spell_constraints import (
    LightningTargetConstraintV1, LogTargetConstraintV1, constrain_spell_placement, action_constraints_contract,
)
from ..training.v4.tensorizer import UniversalObservationTensorizerV4
from .adapter import PartialLiveAdapter
from .tracker import LivePublicTracker


class PartialLivePolicy:
    def __init__(self, checkpoint, *, seed: int | None = None, require_events: bool = True):
        torch.set_num_threads(2)
        self.loaded = load_event_checkpoint(checkpoint)
        self.model = self.loaded.model.eval()
        if not self.model.backbone.config.snapshot_only:
            raise ValueError("live diagnostics require snapshot_only")
        self.adapter = PartialLiveAdapter()
        self.tensorizer = None
        self.identity = None
        self.lightning = LightningTargetConstraintV1()
        self.log = LogTargetConstraintV1()
        self.seed = secrets.randbits(32) if seed is None else seed
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ValueError("live sampling seed must be a uint32")
        self.generator = torch.Generator(device="cpu").manual_seed(self.seed)
        self.require_events = require_events
        self.tracker = None
        self.decision_index = 0

    def observe(self, row: dict):
        identity = (row["battle_epoch"], row["own_side"], tuple(c["card_id"] for c in row["own"]["deck"]))
        if self.identity != identity:
            self.tracker = LivePublicTracker(owner=row["own_side"], card_specs=self.adapter.bundle.card_specs,
                                              timeline=self.adapter.timeline, required=self.require_events)
            self.tensorizer = None
            self.identity = identity
        fresh = self.tracker.consume(row)
        self.adapter.tower_rows(row)
        return fresh

    def infer(self, row: dict) -> dict:
        started = time.perf_counter()
        self.observe(row)
        observation = self.adapter.observation(row)
        owner = row["own_side"]
        bundle = self.adapter.bundle
        deck = tuple(c["card_id"] for c in row["own"]["deck"])
        if self.tensorizer is None:
            self.tensorizer = UniversalObservationTensorizerV4(
                actor_owner=owner, card_catalog=bundle.card_catalog,
                ability_catalog=bundle.ability_catalog, entity_archetype_catalog=bundle.entity_archetype_catalog,
                effect_catalog=bundle.effect_catalog, card_specs=bundle.card_specs,
                deck=deck, tracker=None, deck_roles={card: (False, False) for card in deck},
                config=self.model.backbone.config, reject_unknown_public_semantics=True,
            )
            self.tensorizer.start_episode(observation)
            # The live tracker is already bound to a public event stream. Its
            # unknown opponent initial state must not be reset to a fake exact
            # elixir/hand merely to satisfy the offline reset interface.
            self.tensorizer.tracker = self.tracker
        frame = canonicalize_event_observation(self.tensorizer.tensorize(observation))
        card_ids = row_card_ids(frame, bundle.card_catalog, card_count=8, device=torch.device("cpu"))
        cards = torch.zeros(1, 8, dtype=torch.bool)
        cells = torch.zeros(1, 8, 576, dtype=torch.bool)
        delays = torch.zeros(1, 8, self.model.event_config.delay_bin_count, dtype=torch.bool)
        by_card = {s["card"]["card_id"]: s for s in row["own"]["hand"] if s["card"]}
        for i, card_id in enumerate(card_ids.tolist()):
            hand = by_card.get(card_id)
            if not hand:
                continue
            minimum = minimum_playable_delay_ms(
                elixir=row["own"]["elixir_raw"]/10000, cheapest_cost=hand["card"]["cost"],
                tick=observation.tick, timeline=self.adapter.timeline, end_tick=NATIVE_GAMEPLAY_END_TICK)
            if minimum is None:
                continue
            allowed_delays = torch.tensor(playable_delay_mask(
                self.model.event_config.delay_offset_ms, minimum_delay_ms=minimum,
                tick=observation.tick, end_tick=NATIVE_GAMEPLAY_END_TICK))
            entry = observation.action_mask.placement_masks.get(str(hand["slot"]))
            if entry is None:
                continue
            rows = constrain_spell_placement(
                entry["row_major"], observation, owner, card_id,
                lightning_constraint=self.lightning, log_constraint=self.log,
            )
            cells[0, i] = torch.tensor(event_placement_rows_to_model(rows, owner=owner)).flatten()
            cards[0, i] = cells[0, i].any() & allowed_delays.any()
            cells[0, i] &= cards[0, i]
            delays[0, i] = allowed_delays
        with torch.inference_mode():
            context = self.model.encode(frame, self.model.initial_state(1))
            gate_mask = torch.tensor([[True, bool(cards.any())]])
            uniforms = torch.rand((1, 4), generator=self.generator, dtype=torch.float64)
            step = self.model.select_action(
                context, gate_mask=gate_mask, card_mask=cards, cell_masks=cells, delay_mask=delays,
                temperatures=(1., 1., 1., 1.), sample_uniforms=uniforms,
            )
            # The partial live feed carries no verified ability runtime. Its
            # candidate mask keeps the skill branch closed even for v4 weights.
            if int(step.action.ability_candidate[0]) >= 0:
                raise ValueError("partial live feed lacks verified ability execution support")
            gate_logits, effective_gate_mask = self.model.event_gate(context, gate_mask)
            gate_probs = gate_logits.masked_fill(~effective_gate_mask, -torch.inf).softmax(-1)
        self.decision_index += 1
        missing = [m for m in observation.metadata["missing"] if m != "public_opponent_tracker"]
        result = {"event": "decision", "tick": row["tick"], "sequence": row["sequence"],
                  "battle_epoch": row["battle_epoch"], "own_side": owner,
                  "partial_input": True, "missing": missing,
                  "action_constraints": action_constraints_contract(self.lightning, self.log),
                  "act": bool(step.action.act.item()), "value": float(context.value.item()),
                  "p_act": float(gate_probs[0, 1]), "legal_cards": int(cards.sum()),
                  "sampling": "per-match-cpu-uniform4.v2", "temperatures": [1., 1., 1., 1.],
                  "sampling_seed": self.seed, "decision_index": self.decision_index,
                  "sample_uniforms": uniforms[0].tolist(),
                  "event_watermark": len(self.tracker.cursor.seen),
                  "opponent_elixir_bounds": self.tracker.elixir_interval(1-owner),
                  "inference_ms": (time.perf_counter() - started) * 1000}
        if result["act"]:
            index, cell = int(step.action.card_row.item()), int(step.action.cell.item())
            card_id = int(card_ids[index])
            entry = observation.action_mask.placement_masks[str(by_card[card_id]["slot"])]
            result.update(card_id=card_id, slot=by_card[card_id]["slot"], model_cell=cell,
                          form_code=entry["form_code"], effective_cost=entry["effective_cost"],
                          native_cell=event_cell_to_native(cell, owner=owner, board_width=18),
                          model_subcell_offset=entry.get("model_subcell_offset"),
                          delay_ms=self.model.event_config.bin_to_delay_ms(int(step.action.delay_bin.item())))
            with torch.inference_mode():
                distribution = self.model.delay_logits(context, step.action.card_row, step.action.cell)
                distribution = distribution.masked_fill(~delays[:, index], -torch.inf).softmax(-1)[0]
            result["delay_probabilities"] = distribution.tolist()
            result["selected_delay_probability"] = float(distribution[int(step.action.delay_bin.item())])
        return result
