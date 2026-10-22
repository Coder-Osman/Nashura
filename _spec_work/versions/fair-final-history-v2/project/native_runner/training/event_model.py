"""Event policy: WAIT, delayed card placement, or immediate exact-source ability.

The fixed-step ``UniversalCardPolicyV4`` chooses among the currently legal
candidate cards.  Event decisions instead commit to a *card identity* that may
only become playable at the planned execution tick, so this model reuses the V4
observation encoder and recurrent core but replaces the action decoder with
four event heads:

* gate: ACT versus WAIT for the next event;
* card: one of the actor's own deck rows;
* cell: the 32x18 placement grid, conditioned on the chosen card;
* delay: the frozen sixty-one bin 0..6000 ms vocabulary, conditioned on the
  chosen card *and* the chosen cell.

Card, cell, and delay are one decision, not three independent marginals. The
joint is the autoregressive factorization
``p(gate) p(card) p(cell | card) p(delay | card, cell)``, so a rushes-bridge
card and a dropped-at-the-back card no longer share one timing distribution.
Sampling this in order yields exact joint samples; the cell mask only exists
once the card is known, so that order is forced anyway.

With ``ability_actions=True``, the gate adds ABILITY and a source pointer uses
the native-legal candidate snapshot. Its joint probability is
``p(ABILITY) p(source | ABILITY)``; card/cell/delay terms are absent. WAIT uses
only its gate probability. The four sampling temperatures remain compatible:
the source pointer uses the mutually exclusive card/source temperature.

The V4 action heads are retained inside the backbone for reuse and are simply
unused here; pretraining only optimizes the event heads.
"""

from __future__ import annotations

import os

from collections.abc import Mapping, Sequence

from dataclasses import dataclass, replace
import math

import torch
from torch import Tensor, nn
import torch.nn.functional as F
from torch.distributions import Categorical

from .event_config import EventPolicyConfigV1
from .event_cache import EventObservationRecordV1
from .v4.catalog import CardCatalogV1
from .v4.components import mlp
from .v4.config import ModelConfigV4, scaled_model_config_v4
from .v4.model import UniversalCardPolicyV4, _distribution
from .v4.tensors import (
    PolicyContextV4,
    RecurrentPolicyStateV4,
    UniversalSemanticBatchV4,
)


EVENT_MODEL_VERSION = "event-card-policy-model.v2"
# V4 tensorizer._match_scalars writes absolute True Red identity at index 17.
# Keep its column (and checkpoint weight shapes), but remove the event input signal.
EVENT_ABSOLUTE_OWNER_SCALAR_INDEX = 17


@dataclass(frozen=True, slots=True)
class EventModelConfigV1:
    delay_hidden: int = 256
    cell_hidden: int = 128
    initial_act_probability: float = 0.5
    delay_neighbor_weight: float = 0.1
    residual_blocks: int = 0
    residual_hidden: int = 512
    ability_actions: bool = False
    ability_delay: bool = False

    def __post_init__(self) -> None:
        if type(self.ability_actions) is not bool:
            raise ValueError("ability_actions must be a boolean")
        if type(self.ability_delay) is not bool or (self.ability_delay and not self.ability_actions):
            raise ValueError("ability_delay requires ability_actions")
        if type(self.residual_blocks) is not int or self.residual_blocks < 0:
            raise ValueError("residual block count must be a non-negative integer")
        if type(self.residual_hidden) is not int or self.residual_hidden <= 0:
            raise ValueError("residual hidden width must be a positive integer")
        if self.delay_hidden <= 0 or self.cell_hidden <= 0:
            raise ValueError("event head widths must be positive")
        if not 0.0 < self.initial_act_probability < 1.0:
            raise ValueError("event initial ACT probability must be in (0, 1)")
        if not 0.0 <= self.delay_neighbor_weight < 0.5:
            raise ValueError("delay neighbor weight must be in [0, 0.5)")


@dataclass(slots=True)
class EventPolicyOutputV1:
    gate_logits: Tensor
    card_logits: Tensor
    delay_logits: Tensor
    cell_logits: Tensor
    policy_context: Tensor
    context: PolicyContextV4
    # The backbone already computes this on every forward; PPO's critic reuses
    # it. ``None`` on the streaming-training path, which reconstructs this
    # dataclass from a tensor tuple and never needs the value.
    value: Tensor | None = None


@dataclass(slots=True)
class EventActionV1:
    """One event decision: WAIT, delayed PLAY, or immediate ABILITY.

    Card/cell/delay are -1 on WAIT and ABILITY; ability_candidate is -1 on
    WAIT and PLAY. ``act`` is true for either executable branch.
    """

    act: Tensor
    card_row: Tensor
    cell: Tensor
    delay_bin: Tensor
    ability_candidate: Tensor | None = None

    def __post_init__(self) -> None:
        if self.ability_candidate is None:
            self.ability_candidate = torch.full_like(self.card_row, -1)

    @property
    def gate_choice(self) -> Tensor:
        return torch.where(self.ability_candidate >= 0, 2, self.act.long())


@dataclass(slots=True)
class EventStepV1:
    """Behaviour record of one decided step, shaped for PPO.

    ``log_prob`` is the joint ``log pi(a)`` of the whole action. The per-head
    terms are kept apart because the entropy bonus and the diagnostics want them
    separately, and because the temperature that produced each one has to be
    stored with the rollout: an importance ratio is only meaningful when the
    behaviour distribution behind the stored log-prob is known.
    """

    action: EventActionV1
    log_prob: Tensor
    gate_log_prob: Tensor
    card_log_prob: Tensor
    cell_log_prob: Tensor
    delay_log_prob: Tensor
    gate_entropy: Tensor
    card_entropy: Tensor
    cell_entropy: Tensor
    delay_entropy: Tensor
    temperatures: tuple[float, float, float, float]
    ability_log_prob: Tensor | None = None
    ability_entropy: Tensor | None = None

    def __post_init__(self) -> None:
        if self.ability_log_prob is None:
            self.ability_log_prob = torch.zeros_like(self.log_prob)
        if self.ability_entropy is None:
            self.ability_entropy = torch.zeros_like(self.log_prob)


@dataclass(slots=True)
class EventAbilityContextV1(PolicyContextV4):
    # Derived from the persisted native-legal candidate snapshot, so PPO and
    # collection reconstruct exactly the same source mask (including padding).
    ability_mask: Tensor


@dataclass(slots=True)
class EventTargetsV1:
    gate: Tensor
    card_row: Tensor
    delay_bin: Tensor
    cell: Tensor
    act_mask: Tensor
    cell_mask: Tensor

    @property
    def act_count(self) -> int:
        return int(self.act_mask.sum().item())

    @property
    def cell_count(self) -> int:
        return int(self.cell_mask.sum().item())


class EventCardPolicyV1(nn.Module):
    """Event policy heads over the shared V4 semantic observation encoder."""

    def __init__(
        self,
        backbone: UniversalCardPolicyV4,
        *,
        config: EventModelConfigV1 | None = None,
        event_config: EventPolicyConfigV1 | None = None,
    ) -> None:
        super().__init__()
        self.backbone = backbone
        self.config = config or EventModelConfigV1()
        self.event_config = event_config or EventPolicyConfigV1()
        if self.config.residual_blocks:
            from .event_residual_core import install_stateless_residual_core
            install_stateless_residual_core(
                backbone, blocks=self.config.residual_blocks, hidden=self.config.residual_hidden
            )
        v4 = backbone.config
        if (v4.board_width, v4.board_height) != (
            self.event_config.board_width,
            self.event_config.board_height,
        ):
            raise ValueError("event and backbone arena grids differ")
        self.card_dim = v4.token_dim
        self.card_count = v4.max_own_cards
        self.cell_count = v4.board_width * v4.board_height

        self.gate_head = mlp(v4.lstm_hidden_dim, 256, 2)
        self.card_query = nn.Linear(v4.lstm_hidden_dim, self.card_dim)
        self.wait_embedding = nn.Parameter(torch.empty(self.card_dim))
        nn.init.normal_(self.wait_embedding, std=0.02)
        self.delay_head = mlp(
            v4.lstm_hidden_dim + self.card_dim + self.config.cell_hidden,
            self.config.delay_hidden,
            self.event_config.delay_bin_count,
        )
        self.cell_condition = mlp(v4.lstm_hidden_dim + self.card_dim, 256, self.config.cell_hidden)
        self.cell_film = nn.Linear(self.config.cell_hidden, 2 * v4.spatial_hidden_channels)
        self.cell_key = nn.Conv2d(v4.spatial_hidden_channels, self.config.cell_hidden, kernel_size=1)
        self.cell_query = mlp(v4.lstm_hidden_dim + self.card_dim, 256, self.config.cell_hidden)
        self._initialize_gate_prior()
        self._zero_cell_conditioning_columns()
        if self.config.ability_actions:
            self.ability_gate = nn.Linear(v4.lstm_hidden_dim, 1)
            self.ability_query = nn.Linear(v4.lstm_hidden_dim, self.card_dim)
            nn.init.zeros_(self.ability_gate.weight)
            nn.init.constant_(self.ability_gate.bias, -4.0)

    def _initialize_gate_prior(self) -> None:
        gate = self.gate_head[-1]
        if not isinstance(gate, nn.Linear):
            raise TypeError("event gate head must end in Linear")
        probability = self.config.initial_act_probability
        with torch.no_grad():
            gate.weight.zero_()
            gate.bias.copy_(
                torch.log(torch.tensor((1.0 - probability, probability), dtype=gate.bias.dtype, device=gate.bias.device))
            )

    def _zero_cell_conditioning_columns(self) -> None:
        """Start with the cell input inert.

        A fresh model then reproduces the pre-cell-conditioning delay head
        exactly, and an older checkpoint loads by zero-padding the same trailing
        columns (see :func:`pad_legacy_delay_head`). Training grows them.
        """

        head = self.delay_head[0]
        if not isinstance(head, nn.Linear):
            raise TypeError("event delay head must start with a Linear")
        cell_hidden = self.config.cell_hidden
        if head.in_features <= cell_hidden:
            raise ValueError("event delay head is missing its cell conditioning columns")
        with torch.no_grad():
            head.weight[:, -cell_hidden:].zero_()

    def initial_state(
        self, batch_size: int, *, device: torch.device | str | None = None, dtype: torch.dtype | None = None
    ) -> RecurrentPolicyStateV4:
        return self.backbone.initial_state(batch_size, device=device, dtype=dtype)

    def parameter_count(self) -> int:
        return sum(parameter.numel() for parameter in self.parameters())

    def encode(
        self,
        batch: UniversalSemanticBatchV4,
        state: RecurrentPolicyStateV4,
        *,
        episode_start: Tensor | None = None,
    ) -> PolicyContextV4:
        """Run the shared backbone once.

        Decoders call this a single time and then re-run :meth:`heads` for each
        choice: the event heads are cheap, the backbone is not, and the card and
        cell choices are only known after the gate and card logits exist.
        """

        # Apply before either snapshot or recurrent encoding. Tiny synthetic
        # configs may omit this column altogether. Never mutate a shared batch.
        if batch.match_scalars.shape[-1] > EVENT_ABSOLUTE_OWNER_SCALAR_INDEX:
            scalars = batch.match_scalars.clone()
            scalars[..., EVENT_ABSOLUTE_OWNER_SCALAR_INDEX] = 0
            batch = replace(batch, match_scalars=scalars)
        context = self.backbone.forward(batch, state, episode_start=episode_start, validate=False)
        if self.config.ability_actions:
            from dataclasses import fields
            from .event_abilities import candidate_ability_mask
            context = EventAbilityContextV1(
                **{f.name: getattr(context, f.name) for f in fields(PolicyContextV4)},
                ability_mask=candidate_ability_mask(batch))
        return context

    def event_gate(self, context, gate_mask):
        logits = self.gate_head(context.policy_context)
        if not self.config.ability_actions:
            return logits, gate_mask
        if gate_mask is None:
            gate_mask = torch.ones_like(logits, dtype=torch.bool)
        if gate_mask.shape != logits.shape:
            raise ValueError("event gate mask must describe WAIT/PLAY; ability legality comes from the snapshot")
        return (torch.cat((logits, self.ability_gate(context.policy_context)), -1),
                torch.cat((gate_mask, context.ability_mask.any(-1, keepdim=True)), -1))

    def ability_distribution(self, context, temperature, validate=False):
        logits = torch.einsum("bd,bcd->bc", self.ability_query(context.policy_context),
                              context.encoded.candidate_memory) / math.sqrt(self.card_dim)
        return _distribution(logits, mask=context.ability_mask, temperature=temperature, validate=validate)

    def heads(
        self,
        context: PolicyContextV4,
        *,
        card_row: Tensor | None = None,
        cell: Tensor | None = None,
        cell_mask: Tensor | None = None,
    ) -> EventPolicyOutputV1:
        """Card imitation heads over an encoded context.

        This preserves the card-only supervised target contract. Interactive
        decoders must use ``select_action`` to include the ability branch.

        ``card_row`` and ``cell`` select what the downstream heads condition on.
        Omitted values fall back to the model's own argmax, which is the
        teacher-forcing-free behaviour; decoders that resolve legality masks
        outside the model should pass their resolved choice in, so the delay
        distribution matches the placement actually played.
        """

        card_logits = self.card_logits(context)
        if card_row is None:
            card_row = card_logits.argmax(dim=-1)
        keys, query, condition_input = self._cell_conditioning(context, card_row)
        cell_logits = self._score_cells(query, keys)
        if cell is None:
            ranking = cell_logits
            if cell_mask is not None:
                ranking = ranking.masked_fill(~cell_mask, torch.finfo(ranking.dtype).min)
            cell = ranking.argmax(dim=-1)
        cell_embedding = self._selected_cell_memory(keys, cell)
        return EventPolicyOutputV1(
            gate_logits=self.gate_head(context.policy_context),
            card_logits=card_logits,
            delay_logits=self.delay_head(torch.cat((condition_input, cell_embedding), dim=-1)),
            cell_logits=cell_logits,
            policy_context=context.policy_context,
            context=context,
            value=context.value,
        )

    def card_logits(self, context: PolicyContextV4) -> Tensor:
        """Deck-row logits. Card comes first, so nothing downstream feeds back."""

        return torch.einsum(
            "bd,bcd->bc",
            self.card_query(context.policy_context),
            context.encoded.own_card_memory,
        ) / math.sqrt(self.card_dim)

    def cell_logits(self, context: PolicyContextV4, card_row: Tensor) -> Tensor:
        """Placement logits over the flattened grid, conditioned on the card."""

        keys, query, _ = self._cell_conditioning(context, card_row)
        return self._score_cells(query, keys)

    def delay_logits(self, context: PolicyContextV4, card_row: Tensor, cell: Tensor) -> Tensor:
        """Timing logits, conditioned on the card *and* the chosen cell."""

        keys, _, condition_input = self._cell_conditioning(context, card_row)
        embedding = self._selected_cell_memory(keys, cell)
        return self.delay_head(torch.cat((condition_input, embedding), dim=-1))

    def ability_delay_logits(self, context, candidate):
        """The shared timing head, conditioned on the selected skill source.

        Untargeted abilities have no spatial target, so their cell embedding is
        zero. Candidate memory already contains the ability and source identity.
        """
        memory = context.encoded.candidate_memory
        source = memory[torch.arange(memory.shape[0], device=memory.device), candidate.clamp_min(0)]
        return self.delay_head(torch.cat((context.policy_context, source,
            source.new_zeros((source.shape[0], self.config.cell_hidden))), dim=-1))

    def action_delay_distribution(self, context, card_row, cell, ability_candidate,
                                  delay_mask, temperature, validate=False):
        logits = self.delay_logits(context, card_row, cell.clamp_min(0))
        mask = self._mask_for_card(delay_mask, card_row, label="delay_masks")
        if self.config.ability_delay:
            if ability_candidate is None:
                raise ValueError("delayed ability scoring needs source conditioning")
            skill = ability_candidate >= 0
            logits = torch.where(skill[:, None], self.ability_delay_logits(context, ability_candidate), logits)
            if mask is not None:
                mask = mask | skill[:, None]
        return _distribution(logits, mask=mask, temperature=temperature, validate=validate)

    def _cell_conditioning(self, context: PolicyContextV4, card_row: Tensor) -> tuple[Tensor, Tensor, Tensor]:
        """FiLM-conditioned spatial keys, the shared query, and the delay input."""

        selected = self._selected_card_memory(context.encoded.own_card_memory, card_row)
        condition_input = torch.cat((context.policy_context, selected), dim=-1)
        condition = self.cell_condition(condition_input)
        gamma, beta = self.cell_film(condition).chunk(2, dim=-1)
        conditioned = context.encoded.spatial_memory * (1.0 + gamma[:, :, None, None]) + beta[:, :, None, None]
        return self.cell_key(conditioned), self.cell_query(condition_input), condition_input

    def _score_cells(self, query: Tensor, keys: Tensor) -> Tensor:
        logits = torch.einsum("bd,bdhw->bhw", query, keys) / math.sqrt(query.shape[-1])
        return logits.flatten(1)

    def select_action(
        self,
        context: PolicyContextV4,
        *,
        gate_mask: Tensor | None = None,
        card_mask: Tensor | None = None,
        cell_masks: Tensor | None = None,
        delay_mask: Tensor | None = None,
        temperatures: Sequence[float] = (1.0, 1.0, 1.0, 1.0),
        sample: bool = True,
        validate: bool = False,
        sample_uniforms: Tensor | None = None,
    ) -> EventStepV1:
        """Draw one event action in gate -> card -> cell -> delay order.

        Sampling in that order draws exact samples from the joint
        ``p(gate) p(card) p(cell | card) p(delay | card, cell)`` — unlike a
        greedy cascade, which can settle on a pair that is not the joint mode.
        The order is forced as well as exact: legality is keyed by hand slot, so
        a cell mask only exists once the card is known.

        ``cell_masks`` and ``delay_masks`` are ``[B, card_count, vocabulary]``
        because both depend on the card drawn: the legal placements belong to
        that card's slot, and the earliest deliverable delay follows that card's
        cost. ``gate_mask`` is ``[B, 2]`` over
        ``(WAIT, ACT)`` and exists for the states where acting is impossible
        within the delay vocabulary at all — a short bar is *not* one of them,
        since the contract's delay can carry the play to when elixir arrives.
        ``sample=False`` gives the deterministic decode the renderers use.
        """

        if sample_uniforms is not None:
            if not sample or sample_uniforms.shape != (context.policy_context.shape[0], 4):
                raise ValueError("sample_uniforms requires sampling and shape [B, 4]")
            if not bool(((sample_uniforms >= 0) & (sample_uniforms < 1)).all()):
                raise ValueError("sample_uniforms must be finite in [0, 1)")

        def draw(distribution, head):
            if not sample:
                return distribution.logits.argmax(dim=-1)
            if sample_uniforms is None:
                return distribution.sample()
            # Per-request uniforms keep sampling independent of batch/queue order.
            # Normalize by the computed cumulative total to handle float rounding.
            cdf = distribution.probs.to(torch.float64).cumsum(dim=-1)
            threshold = sample_uniforms[:, head:head+1].to(cdf) * cdf[:, -1:]
            threshold = torch.minimum(threshold, torch.nextafter(cdf[:, -1:], torch.zeros_like(threshold)))
            return (cdf <= threshold).sum(dim=-1).clamp_max(cdf.shape[-1] - 1)

        gate_temp, card_temp, cell_temp, delay_temp = _event_temperatures(temperatures)
        gate_logits, effective_gate_mask = self.event_gate(context, gate_mask)
        gate_dist = _distribution(
            gate_logits, mask=effective_gate_mask, temperature=gate_temp, validate=validate
        )
        chose = draw(gate_dist, 0)
        card_dist = _distribution(self.card_logits(context), mask=card_mask, temperature=card_temp, validate=validate)
        card_row = draw(card_dist, 1)
        cell_dist = _distribution(
            self.cell_logits(context, card_row),
            mask=self._mask_for_card(cell_masks, card_row, label="cell_masks"),
            temperature=cell_temp,
            validate=validate,
        )
        cell = draw(cell_dist, 2)
        ability_dist = self.ability_distribution(context, card_temp, validate) if self.config.ability_actions else None
        # Branches are exclusive; the source uses the same request-local uniform
        # as the card head, keeping the four-uniform wire protocol unchanged.
        ability_candidate = draw(ability_dist, 1) if ability_dist is not None else None
        chosen_source = (torch.where(chose == 2, ability_candidate, -1)
                         if ability_candidate is not None else None)
        delay_dist = self.action_delay_distribution(context, card_row, cell, chosen_source,
                                                     delay_mask, delay_temp, validate)
        delay_bin = draw(delay_dist, 3)
        return _record_event_step(
            gate_dist, card_dist, cell_dist, delay_dist, chose, card_row, cell, delay_bin, temperatures,
            ability_distribution=ability_dist, ability_candidate=ability_candidate,
            ability_delay=self.config.ability_delay,
        )

    def evaluate_actions(
        self,
        context: PolicyContextV4,
        action: EventActionV1,
        *,
        gate_mask: Tensor | None = None,
        card_mask: Tensor | None = None,
        cell_masks: Tensor | None = None,
        delay_mask: Tensor | None = None,
        temperatures: Sequence[float] = (1.0, 1.0, 1.0, 1.0),
        validate: bool = False,
    ) -> EventStepV1:
        """Re-score stored actions under the current policy, for PPO's ratio.

        The conditioning mirrors collection exactly: the stored cell and delay
        were conditioned on the stored card and cell, so re-scoring under any
        other conditioning would produce a meaningless importance ratio. Pass
        the same temperatures that collected the rollout.
        """

        gate_temp, card_temp, cell_temp, delay_temp = _event_temperatures(temperatures)
        if not self.config.ability_actions and bool((action.ability_candidate >= 0).any()):
            raise ValueError("card-only checkpoint cannot score ability actions")
        skill = action.ability_candidate >= 0
        if bool(skill.any()):
            valid = ((action.delay_bin >= 0) & (action.delay_bin < self.event_config.delay_bin_count)
                     if self.config.ability_delay else action.delay_bin == -1)
            if not bool(valid[skill].all()):
                raise ValueError("ability timing disagrees with the checkpoint contract")
        stored_card = action.card_row.clamp_min(0)
        gate_logits, effective_gate_mask = self.event_gate(context, gate_mask)
        gate_dist = _distribution(
            gate_logits, mask=effective_gate_mask, temperature=gate_temp, validate=validate
        )
        card_dist = _distribution(self.card_logits(context), mask=card_mask, temperature=card_temp, validate=validate)
        cell_dist = _distribution(
            self.cell_logits(context, stored_card),
            mask=self._mask_for_card(cell_masks, action.card_row, label="cell_masks"),
            temperature=cell_temp,
            validate=validate,
        )
        delay_dist = self.action_delay_distribution(context, action.card_row, action.cell,
            action.ability_candidate, delay_mask, delay_temp, validate)
        return _record_event_step(
            gate_dist,
            card_dist,
            cell_dist,
            delay_dist,
            action.gate_choice,
            stored_card,
            action.cell,
            action.delay_bin,
            temperatures,
            ability_distribution=(self.ability_distribution(context, card_temp, validate)
                                  if self.config.ability_actions else None),
            ability_candidate=action.ability_candidate,
            ability_delay=self.config.ability_delay,
        )

    def head_log_probs(
        self,
        context: PolicyContextV4,
        *,
        card_row: Tensor,
        cell: Tensor,
        ability_candidate: Tensor | None = None,
        gate_mask: Tensor | None = None,
        card_mask: Tensor | None = None,
        cell_masks: Tensor | None = None,
        delay_mask: Tensor | None = None,
        temperatures: Sequence[float] = (1.0, 1.0, 1.0, 1.0),
        validate: bool = False,
    ) -> tuple[Tensor, Tensor, Tensor, Tensor]:
        """Masked log-softmax of each head, conditioned exactly as collected.

        A KL-to-reference term needs the whole distribution, not just the
        log-prob of the stored action: a policy can drift in directions that the
        sampled action never reveals, and the point of the anchor is to catch
        that drift. Conditioning (card, cell, masks, temperatures) matches
        :meth:`evaluate_actions` so the two distributions are comparable.
        """

        gate_temp, card_temp, cell_temp, delay_temp = _event_temperatures(temperatures)
        stored_card = card_row.clamp_min(0)
        gate_scores, effective_gate_mask = self.event_gate(context, gate_mask)
        gate_logits = _distribution(
            gate_scores, mask=effective_gate_mask, temperature=gate_temp, validate=validate
        ).logits
        card_logits = _distribution(
            self.card_logits(context), mask=card_mask, temperature=card_temp, validate=validate
        ).logits
        cell_logits = _distribution(
            self.cell_logits(context, stored_card),
            mask=self._mask_for_card(cell_masks, card_row, label="cell_masks"),
            temperature=cell_temp,
            validate=validate,
        ).logits
        delay_logits = self.action_delay_distribution(context, card_row, cell, ability_candidate,
            delay_mask, delay_temp, validate).logits
        result = tuple(F.log_softmax(item, dim=-1) for item in (gate_logits, card_logits, cell_logits, delay_logits))
        if self.config.ability_actions:
            result += (self.ability_distribution(context, card_temp, validate).logits,)
        return result

    @staticmethod
    def _mask_for_card(masks: Tensor | None, card_row: Tensor, *, label: str) -> Tensor | None:
        """Pick the chosen card's row out of a per-card mask stack.

        Placement and timing both depend on *which* card was drawn: a legal
        cell belongs to that card's slot, and the earliest deliverable delay
        follows that card's cost. Both masks therefore travel as
        ``[B, card_count, ...]`` and are gathered with the sampled row.
        """

        if masks is None:
            return None
        if masks.ndim != 3 or masks.shape[0] != card_row.shape[0]:
            raise ValueError(f"{label} must be [B, card_count, vocabulary]")
        index = card_row.clamp_min(0)[:, None, None].expand(-1, 1, masks.shape[-1])
        return masks.gather(1, index)[:, 0]

    def forward(
        self,
        batch: UniversalSemanticBatchV4,
        state: RecurrentPolicyStateV4,
        *,
        episode_start: Tensor | None = None,
        card_row: Tensor | None = None,
        cell: Tensor | None = None,
        cell_mask: Tensor | None = None,
    ) -> EventPolicyOutputV1:
        context = self.encode(batch, state, episode_start=episode_start)
        return self.heads(context, card_row=card_row, cell=cell, cell_mask=cell_mask)

    def _selected_card_memory(self, own_card_memory: Tensor, card_row: Tensor) -> Tensor:
        safe = card_row.clamp_min(0)
        gathered = own_card_memory.gather(1, safe[:, None, None].expand(-1, 1, own_card_memory.shape[-1]))[:, 0]
        wait = self.wait_embedding[None].expand_as(gathered)
        return torch.where((card_row >= 0)[:, None], gathered, wait)

    @staticmethod
    def _selected_cell_memory(keys: Tensor, cell: Tensor) -> Tensor:
        """Gather the chosen cell's key vector, the representation the cell head
        scored, so the delay head reads the same placement the cell head chose.

        ``keys`` is ``[B, cell_dim, H, W]`` and ``cell`` holds flat row-major
        indices, matching :meth:`heads`' flattened cell logits.
        """

        flat = keys.flatten(2)
        index = cell.clamp_min(0)[:, None, None].expand(-1, flat.shape[1], 1)
        return flat.gather(2, index)[:, :, 0]


def pad_legacy_delay_head(state: Mapping[str, object], model: EventCardPolicyV1) -> dict[str, object]:
    """Make a pre-cell-conditioning state dict loadable by ``model``.

    The delay head gained ``cell_hidden`` trailing input columns when it started
    conditioning on the chosen cell. Older checkpoints predate them; zero
    padding keeps their behaviour exact, because a zero column contributes
    nothing and the new columns are already zero right after initialization.
    Anything other than that single width change is left for ``load_state_dict``
    to reject.
    """

    key = "delay_head.0.weight"
    expected = model.state_dict()[key]
    legacy = state.get(key)
    if not torch.is_tensor(legacy) or tuple(legacy.shape) == tuple(expected.shape):
        return dict(state)
    if legacy.shape[0] != expected.shape[0] or legacy.shape[1] > expected.shape[1]:
        raise ValueError(f"cannot migrate {key}: {tuple(legacy.shape)} -> {tuple(expected.shape)}")
    padded = legacy.new_zeros(expected.shape)
    padded[:, : legacy.shape[1]] = legacy
    return {**dict(state), key: padded}


def _event_temperatures(temperatures: Sequence[float]) -> tuple[float, float, float, float]:
    """Validate the four per-head sampling temperatures.

    The event decision has one head more than V4's decode — card and cell are
    separate distributions rather than one joint candidate — so four
    temperatures travel with a rollout. A stored log-prob is only meaningful
    together with the temperatures that produced it.
    """

    values = tuple(float(item) for item in temperatures)
    if len(values) != 4 or any(not math.isfinite(item) or item <= 0.0 for item in values):
        raise ValueError("temperatures must be four finite positive values (gate, card, cell, delay)")
    return values


def _record_event_step(
    gate_distribution: Categorical,
    card_distribution: Categorical,
    cell_distribution: Categorical,
    delay_distribution: Categorical,
    gate_choice: Tensor,
    card_row: Tensor,
    cell: Tensor,
    delay_bin: Tensor,
    temperatures: Sequence[float],
    *,
    ability_distribution: Categorical | None = None,
    ability_candidate: Tensor | None = None,
    ability_delay: bool = False,
) -> EventStepV1:
    """Assemble the behaviour record, keeping WAIT rows free of head terms.

    Waiting makes no card, placement or timing decision, so those three terms
    stay zero there and only the gate term is always live — the same
    convention :func:`event_il_loss` uses when it supervises ACT rows only.
    """

    act = gate_choice.to(torch.bool)
    play = gate_choice == 1
    skill = gate_choice == 2
    active = play.to(gate_distribution.logits.dtype)
    gate_log_prob = gate_distribution.log_prob(gate_choice)
    card_log_prob = card_distribution.log_prob(card_row.clamp_min(0)) * active
    cell_log_prob = cell_distribution.log_prob(cell.clamp_min(0)) * active
    timed = (play | skill) if ability_delay else play
    delay_log_prob = delay_distribution.log_prob(delay_bin.clamp_min(0)) * timed
    ability_log_prob = torch.zeros_like(gate_log_prob)
    ability_entropy = torch.zeros_like(gate_log_prob)
    if ability_distribution is not None:
        ability_log_prob = ability_distribution.log_prob(ability_candidate.clamp_min(0)) * skill
        ability_entropy = ability_distribution.entropy() * skill
    elif bool(skill.any()):
        raise ValueError("ability action requires an ability distribution")
    return EventStepV1(
        action=EventActionV1(
            act=act,
            card_row=torch.where(play, card_row, torch.full_like(card_row, -1)),
            cell=torch.where(play, cell, torch.full_like(cell, -1)),
            delay_bin=torch.where(timed, delay_bin, torch.full_like(delay_bin, -1)),
            ability_candidate=(torch.where(skill, ability_candidate, torch.full_like(ability_candidate, -1))
                               if ability_candidate is not None else None),
        ),
        log_prob=gate_log_prob + card_log_prob + cell_log_prob + delay_log_prob + ability_log_prob,
        gate_log_prob=gate_log_prob,
        card_log_prob=card_log_prob,
        cell_log_prob=cell_log_prob,
        delay_log_prob=delay_log_prob,
        gate_entropy=gate_distribution.entropy(),
        card_entropy=card_distribution.entropy() * active,
        cell_entropy=cell_distribution.entropy() * active,
        delay_entropy=delay_distribution.entropy() * timed,
        temperatures=_event_temperatures(temperatures),
        ability_log_prob=ability_log_prob,
        ability_entropy=ability_entropy,
    )


def build_event_targets(
    batch: UniversalSemanticBatchV4,
    records: list[EventObservationRecordV1] | tuple[EventObservationRecordV1, ...],
    catalog: CardCatalogV1,
    *,
    event_config: EventPolicyConfigV1 | None = None,
) -> EventTargetsV1:
    """Align event labels to deck rows, delay bins, and grid cells."""

    actual = event_config or EventPolicyConfigV1()
    if len(records) != batch.batch_size:
        raise ValueError("event records and observation batch must align")
    device = batch.own_cards.mask.device
    gate = torch.zeros(len(records), dtype=torch.long, device=device)
    card_row = torch.full((len(records),), -1, dtype=torch.long, device=device)
    delay_bin = torch.zeros(len(records), dtype=torch.long, device=device)
    cell = torch.full((len(records),), -1, dtype=torch.long, device=device)
    card_rows = batch.own_cards.card_vocab_id
    card_mask = batch.own_cards.mask
    for index, record in enumerate(records):
        if record.wait:
            continue
        gate[index] = 1
        if record.card_id is not None:
            vocab = catalog.vocab_id(int(record.card_id))
            matches = (card_mask[index] & (card_rows[index] == vocab)).nonzero(as_tuple=False).flatten()
            if matches.numel():
                card_row[index] = int(matches[0].item())
        delay_bin[index] = min(max(int(record.delay_bin), 0), actual.delay_bin_count - 1)
        if record.target_cell is not None:
            x, y = record.target_cell
            cell[index] = int(y) * actual.board_width + int(x)
    act_mask = (gate == 1) & (card_row >= 0)
    cell_mask = act_mask & (cell >= 0) & (cell < actual.board_width * actual.board_height)
    return EventTargetsV1(
        gate=gate,
        card_row=card_row,
        delay_bin=delay_bin,
        cell=cell,
        act_mask=act_mask,
        cell_mask=cell_mask,
    )


def _smoothed_delay_target(delay_bin: Tensor, bin_count: int, weight: float) -> Tensor:
    target = F.one_hot(delay_bin, num_classes=bin_count).to(torch.float32)
    if weight <= 0.0:
        return target
    left = torch.roll(target, shifts=1, dims=-1)
    right = torch.roll(target, shifts=-1, dims=-1)
    left[..., 0] = 0.0
    right[..., -1] = 0.0
    neighbor = (left + right)
    neighbor_sum = neighbor.sum(dim=-1, keepdim=True).clamp_min(1.0)
    return (1.0 - weight) * target + weight * (neighbor / neighbor_sum)


def event_il_loss(
    output: EventPolicyOutputV1,
    targets: EventTargetsV1,
    *,
    event_config: EventPolicyConfigV1 | None = None,
    delay_neighbor_weight: float = 0.1,
    delay_coarse_weight: float = 0.0,
    delay_coarse_group: int = 5,
) -> tuple[Tensor, dict[str, Tensor]]:
    """Supervised loss for event pretraining.

    ACT rows supervise the card, delay, and cell heads; every row supervises the
    gate.  Rows whose label card is absent from the actor deck (for example a
    Mirror copy) keep only the gate loss.

    ``delay_coarse_weight`` adds a coarse auxiliary cross-entropy over
    ``delay_coarse_group`` adjacent fine bins (500 ms groups by default).  The
    fine 61-way target is noisy and hard to fit directly; the coarse term gives
    the shared delay representation a much easier gradient before the fine head
    refines it.
    """

    actual = event_config or EventPolicyConfigV1()
    gate_loss = F.cross_entropy(output.gate_logits, targets.gate)
    zero = gate_loss.new_zeros(())
    parts: dict[str, Tensor] = {
        "gate": gate_loss,
        "card": zero,
        "delay": zero,
        "delay_coarse": zero,
        "cell": zero,
    }
    act_count = targets.act_count
    if act_count:
        card_loss = F.cross_entropy(output.card_logits, targets.card_row.clamp_min(0), reduction="none")
        parts["card"] = (card_loss * targets.act_mask).sum() / act_count
        distribution = _smoothed_delay_target(targets.delay_bin, actual.delay_bin_count, delay_neighbor_weight)
        log_prob = F.log_softmax(output.delay_logits, dim=-1)
        delay_loss = -(log_prob * distribution).sum(dim=-1)
        parts["delay"] = (delay_loss * targets.act_mask).sum() / act_count
        if delay_coarse_weight > 0.0 and delay_coarse_group > 1:
            group = int(delay_coarse_group)
            bin_count = actual.delay_bin_count
            groups = (bin_count + group - 1) // group
            pad = groups * group - bin_count
            logits = output.delay_logits
            if pad:
                logits = torch.cat(
                    (logits, logits.new_full((logits.shape[0], pad), float("-inf"))), dim=-1
                )
            coarse_logits = logits.reshape(logits.shape[0], groups, group).logsumexp(dim=-1)
            coarse_target = (targets.delay_bin // group).clamp_max(groups - 1)
            coarse_loss = F.cross_entropy(coarse_logits, coarse_target, reduction="none")
            parts["delay_coarse"] = (coarse_loss * targets.act_mask).sum() / act_count
    cell_count = targets.cell_count
    if cell_count:
        cell_loss = F.cross_entropy(output.cell_logits, targets.cell.clamp_min(0), reduction="none")
        parts["cell"] = (cell_loss * targets.cell_mask).sum() / cell_count
    total = (
        parts["gate"]
        + parts["card"]
        + parts["delay"]
        + delay_coarse_weight * parts["delay_coarse"]
        + parts["cell"]
    )
    return total, parts



def event_backbone_config_v4() -> ModelConfigV4:
    """Backbone capacity for the event policy.

    ``CR_EVENT_MODEL_SCALE`` selects a widened backbone (for example ``1.45``
    for roughly twice the parameters); the unset default is the production
    configuration, so existing checkpoints and runs are unchanged.
    """

    raw = os.environ.get("CR_EVENT_MODEL_SCALE", "").strip()
    snapshot_only = os.environ.get("CR_EVENT_MODEL_SNAPSHOT_ONLY", "").strip() not in ("", "0", "false", "False")
    if not raw or abs(float(raw) - 1.0) < 1e-9:
        base = ModelConfigV4()
        return replace(base, snapshot_only=True) if snapshot_only and not base.snapshot_only else base
    factor = float(raw)
    if factor <= 0.0:
        raise ValueError("CR_EVENT_MODEL_SCALE must be positive")
    scaled = scaled_model_config_v4(factor)
    return replace(scaled, snapshot_only=True) if snapshot_only and not scaled.snapshot_only else scaled


def build_event_model_v1(
    *,
    device: torch.device | str | None = None,
    model_config: EventModelConfigV1 | None = None,
    event_config: EventPolicyConfigV1 | None = None,
) -> EventCardPolicyV1:
    """Construct the production event policy over the shared V4 backbone."""

    from .v4.factory import production_semantic_bundle

    bundle = production_semantic_bundle()
    backbone = UniversalCardPolicyV4(
        bundle.card_catalog,
        ability_catalog=bundle.ability_catalog,
        entity_archetype_catalog=bundle.entity_archetype_catalog,
        effect_catalog=bundle.effect_catalog,
        mechanic_profile_catalog=bundle.mechanic_profile_catalog,
        config=event_backbone_config_v4(),
    )
    model = EventCardPolicyV1(backbone, config=model_config, event_config=event_config)
    return model if device is None else model.to(device)
