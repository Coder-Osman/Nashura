"""Stateless SSPI policy/value model using the frozen V4 FAIR feature schema."""

from __future__ import annotations

from dataclasses import asdict
import math

import torch
from torch import nn

from ..az_joint.model import SemanticEncoder
from ..fair_search.contracts import semantic_digest
from ..v4.config import ModelConfigV4
from ..v4.factory import production_semantic_bundle
from .protocol import (ABILITY_FEATURE_DIM, ABILITY_FEATURE_SCHEMA, ActionMode, DELAY_MS,
    HandState, SSPILegality, SSPIAction, stable_card_rows)


HAND_FEATURE_SCHEMA = "sspi-eight-card-readiness-and-refill-time.v2"
HAND_FEATURE_DIM = 128


def _hand_features(hand: HandState, *, device):
    if not isinstance(hand, HandState):
        raise TypeError("SSPI model requires a native stable-hand snapshot")
    card_row = {card_id: slot for slot, card_id in hand.deck}
    ready_slots = {slot.card_id: slot.index for slot in hand.slots if slot.card_id is not None}
    cycle_positions = {card_id: index for index, (_, card_id) in enumerate(hand.cycle)}
    pending = {refill.card_id: refill for refill in hand.pending_refills}
    values = []
    for _, card_id in hand.deck:
        ready_slot = ready_slots.get(card_id)
        queue_position = cycle_positions.get(card_id)
        refill = pending.get(card_id)
        values.append(float(ready_slot is not None))
        values.extend(float(ready_slot == index) for index in range(4))
        values.extend(float(queue_position == index) for index in range(8))
        values.append(float(refill is not None and refill.time_known))
        values.append(min(refill.remaining_ticks or 0, 1200) / 1200. if refill is not None else 0.)
    next_slot = card_row.get(hand.known_next_card_id)
    values.extend(float(next_slot == index) if next_slot is not None else 0. for index in range(8))
    if len(values) != HAND_FEATURE_DIM:
        raise AssertionError("SSPI hand feature width changed")
    return torch.tensor(values, dtype=torch.float32, device=device)


class _ZeroSummary(nn.Module):
    """Drop historical event and previous-action tensors without learned parameters."""

    def __init__(self, width: int):
        super().__init__()
        self.width = width

    def forward(self, values, **_kwargs):
        mask = getattr(values, "mask", None)
        if mask is None:
            mask = getattr(values, "action_mask", None)
        if mask is None:
            raise ValueError("history summary requires an explicit batch mask")
        return torch.zeros((mask.shape[0], self.width), dtype=torch.float32, device=mask.device)


class _ResidualBlock(nn.Module):
    def __init__(self):
        super().__init__()
        self.norm = nn.LayerNorm(512)
        self.layers = nn.Sequential(nn.Linear(512, 512), nn.SiLU(), nn.Linear(512, 512))

    def forward(self, value):
        return value + self.layers(self.norm(value))


class SSPIConditionalHeads(nn.Module):
    def __init__(self, *, token_width=256, spatial_width=64):
        super().__init__()
        self.mode = nn.Sequential(nn.Linear(512, 256), nn.SiLU(), nn.Linear(256, 3))
        self.card_query = nn.Linear(512, token_width)
        self.cell_query = nn.Sequential(nn.Linear(512 + token_width, 256), nn.SiLU(), nn.Linear(256, 128))
        self.cell_key = nn.Conv2d(spatial_width, 128, 1)
        self.delay = nn.Sequential(nn.Linear(512 + token_width + 128, 256), nn.SiLU(),
            nn.Linear(256, len(DELAY_MS)))
        # The tagged ABI branch is reserved now. HOG legality keeps it empty.
        self.ability_query = nn.Linear(512, 128)
        self.ability_key = nn.Linear(128, 128)

    def logits_for(self, shared, cards, spatial, ability_features):
        if (shared.shape != (512,) or cards.ndim != 2 or spatial.ndim != 3
                or ability_features.ndim != 2 or ability_features.shape[1] != ABILITY_FEATURE_DIM):
            raise ValueError("SSPI action heads expect one encoded decision")
        keys = self.cell_key(spatial.unsqueeze(0)).squeeze(0).flatten(1).transpose(0, 1)

        def logits(prefix):
            if not prefix:
                return self.mode(shared)
            if prefix == (ActionMode.PLAY,):
                return cards @ self.card_query(shared) / math.sqrt(cards.shape[-1])
            if prefix == (ActionMode.ABILITY,):
                candidate_keys = self.ability_key(ability_features.to(device=shared.device, dtype=shared.dtype))
                return candidate_keys @ self.ability_query(shared) / math.sqrt(candidate_keys.shape[-1])
            if prefix[0] != ActionMode.PLAY or len(prefix) not in (2, 3):
                raise ValueError("invalid SSPI conditional head prefix")
            selected_card = cards[int(prefix[1])]
            context = torch.cat((shared, selected_card))
            if len(prefix) == 2:
                return keys @ self.cell_query(context) / math.sqrt(keys.shape[-1])
            return self.delay(torch.cat((context, keys[int(prefix[2])])))

        return logits


class SSPIPolicy:
    """A legal-prefix categorical distribution over tagged actions."""

    def __init__(self, logits, legality: SSPILegality):
        self.logits = logits
        self.legality = legality
        # A policy is created for one immutable forward pass. Cache per-prefix
        # normalizers: root candidate generation asks for identical card/cell
        # probabilities repeatedly. Cached tensors keep their autograd graph.
        self._conditional_cache = {}

    def conditionals(self, prefix=()):
        prefix = tuple(prefix)
        if prefix in self._conditional_cache:
            return self._conditional_cache[prefix]
        mask = self.legality.mask(prefix)
        logits = self.logits(prefix)
        mask = mask.to(logits.device)
        if logits.shape != mask.shape:
            raise ValueError("SSPI conditional logits differ from their legal prefix")
        if not bool(mask.any()):
            raise ValueError("cannot normalize an empty SSPI action branch")
        if not bool(torch.isfinite(logits[mask]).all()):
            raise ValueError("nonfinite legal SSPI action logit")
        result = logits.masked_fill(~mask, -torch.inf).log_softmax(-1)
        self._conditional_cache[prefix] = result
        return result

    def log_prob(self, action: SSPIAction):
        if not self.legality.legal(action):
            raise ValueError("joint SSPI log probability requires a legal action")
        mode_log = self.conditionals()[int(action.mode)]
        if action.mode == ActionMode.WAIT:
            return mode_log
        if action.mode == ActionMode.ABILITY:
            candidate = next(index for index, item in enumerate(self.legality.ability_candidates)
                if (item.source_entity_id, item.ability_id) == (action.source_entity_id, action.ability_id))
            return mode_log + self.conditionals((ActionMode.ABILITY,))[candidate]
        card_index = self.legality.card_ids.index(action.card_id)
        return (mode_log + self.conditionals((ActionMode.PLAY,))[card_index]
            + self.conditionals((ActionMode.PLAY, card_index))[action.cell]
            + self.conditionals((ActionMode.PLAY, card_index, action.cell))[action.delay_index])

    def sample(self, generator: torch.Generator, *, greedy=False):
        def choose(prefix):
            log_prob = self.conditionals(prefix).detach().cpu()
            if greedy:
                return int(log_prob.argmax())
            return int(torch.multinomial(log_prob.exp(), 1, generator=generator))

        mode = choose(())
        if mode == ActionMode.WAIT:
            action = SSPIAction.wait()
        elif mode == ActionMode.PLAY:
            card_index = choose((ActionMode.PLAY,))
            cell = choose((ActionMode.PLAY, card_index))
            delay = choose((ActionMode.PLAY, card_index, cell))
            action = SSPIAction.play(self.legality.card_ids[card_index], cell, delay)
        else:
            candidate = choose((ActionMode.ABILITY,))
            item = self.legality.ability_candidates[candidate]
            action = SSPIAction.ability(item.source_entity_id, item.ability_id)
        return action, self.log_prob(action)


class SSPIModel(nn.Module):
    """Fixed stateless V4 semantic/spatial backbone with three modes and five delays."""

    schema = "sspi-policy-value.v2"
    backbone_fields = ("scene_state", "spatial_summary", "scalar_summary", "candidate_summary")

    def __init__(self, *, bundle=None, encoder_config=None):
        super().__init__()
        config = encoder_config or ModelConfigV4(snapshot_only=True)
        if not config.snapshot_only:
            raise ValueError("SSPI input must be a single current-state snapshot")
        self.encoder_config = config
        self.encoder = SemanticEncoder(bundle or production_semantic_bundle(), config)
        self.encoder.event_encoder = _ZeroSummary(config.event_summary_dim)
        self.encoder.previous_action_encoder = _ZeroSummary(config.previous_action_dim)
        self.hand_projection = nn.Sequential(nn.Linear(HAND_FEATURE_DIM, 128), nn.SiLU(),
            nn.Linear(128, 128), nn.LayerNorm(128))
        self.trunk = nn.Sequential(nn.Linear(640, 512), nn.SiLU(), _ResidualBlock(), _ResidualBlock(),
            nn.LayerNorm(512))
        self.policy_head = SSPIConditionalHeads(token_width=config.token_dim,
            spatial_width=config.spatial_hidden_channels)
        self.wdl_head = nn.Sequential(nn.Linear(512, 256), nn.SiLU(), nn.Linear(256, 3))

    @property
    def architecture(self):
        return dict(schema=self.schema, encoder=asdict(self.encoder_config),
            backbone_fields=list(self.backbone_fields), history_fields=[],
            recurrent_core=None, mode_order=["WAIT", "PLAY", "ABILITY"], value_order=["WIN", "DRAW", "LOSS"],
            delay_ms=list(DELAY_MS), ability_interface="tagged-source-ability-pointer.v1",
            ability_feature_schema=ABILITY_FEATURE_SCHEMA, ability_feature_dim=ABILITY_FEATURE_DIM,
            hand_feature_schema=HAND_FEATURE_SCHEMA, hand_feature_dim=HAND_FEATURE_DIM,
            ability_candidates_during_hog=0,
            catalogs={name: getattr(self.encoder, name).catalog_id for name in
                ("catalog", "ability_catalog", "entity_archetype_catalog", "effect_catalog", "mechanic_profile_catalog")})

    @property
    def architecture_sha(self):
        return semantic_digest(self.architecture)

    @property
    def parameter_count(self):
        return sum(parameter.numel() for parameter in self.parameters())

    def encode(self, decision):
        hand = getattr(decision, "hand", None)
        frame = getattr(decision, "frame", decision)
        if hand is None:
            raise ValueError("SSPI inference requires FAIR input with a stable hand/FIFO snapshot")
        device = next(self.parameters()).device
        frame = frame.to_model_input(device, float_dtype=torch.float32)
        if frame.batch_size < 1:
            raise ValueError("SSPI encoder requires at least one FAIR frame")
        encoded = self.encoder(frame)
        hand_summary = self.hand_projection(_hand_features(hand, device=device).unsqueeze(0))
        combined = torch.cat([encoded.scene_state, encoded.spatial_summary,
            encoded.scalar_summary + hand_summary, encoded.candidate_summary], dim=-1)
        if combined.shape[-1] != 640:
            raise ValueError("SSPI frozen feature projection width changed")
        return self.trunk(combined), encoded, frame

    def forward(self, decision, legalities):
        shared, encoded, model_frame = self.encode(decision)
        if len(legalities) != shared.shape[0]:
            raise ValueError("one SSPI legality contract per FAIR frame is required")
        policies = []
        for index, legality in enumerate(legalities):
            rows = stable_card_rows(model_frame, self.encoder.catalog, legality.card_ids, index=index)
            ability_features = torch.tensor([candidate.features for candidate in legality.ability_candidates],
                dtype=shared.dtype, device=shared.device).reshape(-1, ABILITY_FEATURE_DIM)
            logits = self.policy_head.logits_for(shared[index], encoded.own_card_memory[index, list(rows)],
                encoded.spatial_memory[index], ability_features)
            policies.append(SSPIPolicy(logits, legality))
        return policies, self.wdl_head(shared)

    def value_only(self, decision):
        shared, _, _ = self.encode(decision)
        return self.wdl_head(shared)


def initialize_fresh_model(seed: int, *, device="cpu", bundle=None, encoder_config=None):
    if type(seed) is not int or seed < 0:
        raise ValueError("fresh SSPI initialization requires an explicit nonnegative seed")
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(seed)
        model = SSPIModel(bundle=bundle, encoder_config=encoder_config)
        nn.init.normal_(model.wdl_head[-1].weight, mean=0., std=1e-3)
        nn.init.zeros_(model.wdl_head[-1].bias)
    if any("event_encoder" in name or "previous_action_encoder" in name or "lstm" in name.lower()
            for name, _ in model.named_parameters()):
        raise AssertionError("SSPI model unexpectedly contains history or recurrent parameters")
    return model.to(device)
