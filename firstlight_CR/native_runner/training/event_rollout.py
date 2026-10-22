"""Assemble a match's decisions into PPO-ready rows.

The event contract settles credit at the *next* decision: a step's transition is
the interval until that actor decides again, including personal process rewards
and the terminal result (win/loss plus margin). This recorder owns that bookkeeping
so the driver's loop stays about driving the engine, and so the semi-Markov
structure the design depends on is testable without an engine.

Rows are kept per owner. Mirror self-play puts one policy on both sides, but a
PPO lane is still one actor's decision sequence — credit must not jump between
the two perspectives mid-lane.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import torch
from typing import Mapping, Sequence

from torch import Tensor

from .event_cache import EventRlRecordV1, write_event_rl_shard
from .event_model import EventActionV1, EventStepV1
from .v4.tensors import UniversalSemanticBatchV4

EXECUTION_CREDIT_CONTRACT = "executed-act-or-wait.v1"


@dataclass(slots=True)
class _PendingV1:
    owner: int
    observation_tick: int
    frame: UniversalSemanticBatchV4
    action: EventActionV1
    step: EventStepV1
    card_mask: Tensor
    cell_masks: Tensor
    delay_mask: Tensor
    gate_mask: Tensor
    decision_ticks: int = 0
    reward: float = 0.0
    base_reward: float = 0.0
    overflow_penalty: float = 0.0
    done: bool = False
    truncated: bool = False
    execution_status: str = "pending"
    action_id: str | None = None
    exclusion_reason: str | None = None
    closed_index: int | None = None
    tower_state: tuple | None = None


@dataclass(slots=True)
class EventLaneRecorderV1:
    """Collect one match's behaviour rows, closing them on the next decision."""

    match_id: str
    policy_version: str
    temperatures: tuple[float, float, float, float]
    decks: Mapping[int, Sequence[int]]
    behavior_checkpoint_sha256: str | None = None
    behavior_model_contract_hash: str | None = None
    action_constraints: Mapping[str, object] | None = None
    reward_config: Mapping[str, object] | None = None
    _pending: dict[int, _PendingV1 | None] = field(default_factory=lambda: {0: None, 1: None})
    _rows: dict[int, list[EventRlRecordV1]] = field(default_factory=lambda: {0: [], 1: []})
    _frames: dict[int, list[UniversalSemanticBatchV4]] = field(default_factory=lambda: {0: [], 1: []})
    # Tracked separately from the rows: a match result must not disappear from
    # the win-rate telemetry just because an owner happened to have no open row.
    _terminal: dict[int, float] = field(default_factory=lambda: {0: 0.0, 1: 0.0})
    _submitted: dict[str, _PendingV1] = field(default_factory=dict)
    _action_ids: set[str] = field(default_factory=set)
    # Legality is not recoverable from the observation tensors, so each row keeps
    # the masks it was sampled under and they travel into the shard.
    _row_masks: dict[int, list[tuple[Tensor, Tensor, Tensor, Tensor]]] = field(
        default_factory=lambda: {0: [], 1: []}
    )

    def decide(
        self,
        *,
        owner: int,
        tick: int,
        frame: UniversalSemanticBatchV4,
        step: EventStepV1,
        card_mask: Tensor,
        cell_masks: Tensor,
        delay_mask: Tensor,
        gate_mask: Tensor,
        tower_state: tuple | None = None,
    ) -> None:
        """Open a behaviour row for one actor's decision at ``tick``."""

        if owner not in (0, 1):
            raise ValueError("event lane recorder needs owner 0 or 1")
        if frame.batch_size != 1:
            raise ValueError("event lane frames must have batch size one")
        self._close(owner, truncated=False)
        self._pending[owner] = _PendingV1(
            owner=owner,
            observation_tick=int(tick),
            frame=frame,
            action=step.action,
            step=step,
            card_mask=card_mask,
            cell_masks=cell_masks,
            delay_mask=delay_mask,
            gate_mask=gate_mask,
            execution_status="pending" if bool(step.action.act[0]) else "wait",
            tower_state=tower_state,
        )

    def cancel(self, owner: int, *, reason: str) -> None:
        """Exclude a Python-side plan without changing its sampled action."""
        pending = self._pending[owner]
        if pending is not None and pending.execution_status == "pending":
            pending.execution_status = "cancelled"
            pending.exclusion_reason = reason

    def submit(self, owner: int, *, tick: int, action_id: str) -> None:
        pending = self._pending[owner]
        if (pending is None or pending.observation_tick != tick
                or pending.execution_status != "pending"
                or not action_id or action_id in self._action_ids):
            raise ValueError("submission does not identify one pending ACT decision")
        pending.execution_status = "submitted"
        pending.action_id = action_id
        self._submitted[action_id] = pending
        self._action_ids.add(action_id)

    def reconcile(self, outcomes) -> None:
        """Only a native action_executed receipt makes an ACT trainable.

        The ledger validates all owners, including the frozen opponent. This
        recorder binds receipts only to the learner's own submitted decisions.
        A receipt can also resolve a row already closed by a public event.
        """
        for event in outcomes:
            identity = event.data.get("action_id")
            pending = self._submitted.get(identity)
            if pending is None:
                continue
            if event.owner != pending.owner:
                raise ValueError("execution receipt owner mismatch")
            if event.event_type == "action_executed":
                candidate = int(pending.action.ability_candidate[0])
                if candidate >= 0:
                    source = int(pending.frame.candidates.native_source_entity[0, candidate])
                    if (getattr(event, "entity_id", None) != source
                            or event.data.get("kind") != "activate_ability"
                            or not event.data.get("fair_ability_activation_exact")):
                        raise ValueError("ability execution credit requires native source attestation")
                status, reason = "executed", None
            elif event.event_type in {"action_rejected", "action_canceled_terminal"}:
                status, reason = "cancelled", event.event_type
            else:
                raise ValueError("unexpected native execution receipt")
            self._submitted.pop(identity)
            pending.execution_status, pending.exclusion_reason = status, reason
            if pending.closed_index is not None:
                rows = self._rows[pending.owner]
                rows[pending.closed_index] = replace(
                    rows[pending.closed_index], execution_status=status, exclusion_reason=reason)

    def advance(
        self,
        *,
        ticks: int,
        rewards: Mapping[int, float] | None = None,
        overflow_penalties: Mapping[int, float] | None = None,
        done: bool = False,
        truncated: bool = False,
    ) -> None:
        """Extend every open transition and close the ones the episode ends on.

        Only ``done``/``truncated`` close a row here; a normal step just accrues
        ticks and reward until the actor decides again.
        """

        if ticks < 0:
            raise ValueError("an event transition cannot run backwards")
        rewards = rewards or {}
        overflow_penalties = overflow_penalties or {}
        if overflow_penalties and self.reward_config is None:
            raise ValueError("personal process reward requires a versioned configuration")
        for owner in (0, 1):
            pending = self._pending[owner]
            if pending is not None:
                pending.decision_ticks += int(ticks)
                base = float(rewards.get(owner, 0.0))
                penalty = float(overflow_penalties.get(owner, 0.0))
                pending.base_reward += base
                pending.overflow_penalty += penalty
                pending.reward += base-penalty
            if not (done or truncated):
                continue
            # The result is a property of the match, not of whether this owner
            # happened to have a row open, so it is tracked for both sides.
            self._terminal[owner] += float(rewards.get(owner, 0.0))
            if pending is not None:
                pending.done = bool(done)
                self._close(owner, truncated=truncated)

    def finish(self, *, truncated: bool = True) -> None:
        """Close everything still open, e.g. when the driver hits its tick cap."""

        for owner in (0, 1):
            self._close(owner, truncated=truncated)

    def _close(self, owner: int, *, truncated: bool) -> None:
        pending = self._pending[owner]
        if pending is None:
            return
        if truncated:
            pending.truncated = True
        if pending.execution_status == "pending":
            self.cancel(owner, reason="truncated" if truncated else (
                "terminal_before_execution" if pending.done else "interrupted"))
        elif pending.execution_status == "submitted":
            pending.execution_status = "unconfirmed"
            pending.exclusion_reason = "missing_execution_receipt"
        self._pending[owner] = None
        step, action = pending.step, pending.action
        pending.closed_index = len(self._rows[owner])
        self._rows[owner].append(
            EventRlRecordV1(
                match_id=self.match_id,
                owner=owner,
                observation_tick=pending.observation_tick,
                decision_ticks=pending.decision_ticks,
                act=bool(action.act[0]),
                card_row=int(action.card_row[0]),
                cell=int(action.cell[0]),
                delay_bin=int(action.delay_bin[0]),
                ability_candidate=int(action.ability_candidate[0]),
                ability_source_entity=(int(pending.frame.candidates.native_source_entity[0, action.ability_candidate[0]])
                                       if int(action.ability_candidate[0]) >= 0 else None),
                ability_vocab_id=(int(pending.frame.candidates.ability_vocab_id[0, action.ability_candidate[0]])
                                  if int(action.ability_candidate[0]) >= 0 else None),
                ability_log_prob=float(step.ability_log_prob[0]),
                ability_entropy=float(step.ability_entropy[0]),
                log_prob=float(step.log_prob[0]),
                gate_log_prob=float(step.gate_log_prob[0]),
                card_log_prob=float(step.card_log_prob[0]),
                cell_log_prob=float(step.cell_log_prob[0]),
                delay_log_prob=float(step.delay_log_prob[0]),
                gate_entropy=float(step.gate_entropy[0]),
                card_entropy=float(step.card_entropy[0]),
                cell_entropy=float(step.cell_entropy[0]),
                delay_entropy=float(step.delay_entropy[0]),
                temperatures=tuple(step.temperatures),
                reward=pending.reward,
                base_reward=pending.base_reward if self.reward_config is not None else None,
                overflow_penalty=pending.overflow_penalty,
                # Only a real result sets done; closing a row because the actor
                # decided again must NOT cut the GAE trace between steps.
                done=pending.done,
                truncated=pending.truncated,
                policy_version=self.policy_version,
                behavior_checkpoint_sha256=self.behavior_checkpoint_sha256,
                behavior_model_contract_hash=self.behavior_model_contract_hash,
                execution_status=pending.execution_status,
                action_id=pending.action_id,
                exclusion_reason=pending.exclusion_reason,
                deck=tuple(int(card) for card in self.decks[owner]),
                opponent_deck=tuple(int(card) for card in self.decks[1 - owner]),
                tower_state=pending.tower_state,
            )
        )
        self._frames[owner].append(pending.frame)
        self._row_masks[owner].append(
            (pending.card_mask, pending.cell_masks, pending.delay_mask, pending.gate_mask)
        )

    @property
    def rows(self) -> dict[int, list[EventRlRecordV1]]:
        return self._rows

    @property
    def open_rows(self) -> int:
        return sum(1 for owner in (0, 1) if self._pending[owner] is not None)

    def lane_count(self, owner: int) -> int:
        return len(self._rows[owner])

    def results(self) -> dict[int, float]:
        """Terminal reward per owner, for win-rate bookkeeping."""

        return dict(self._terminal)

    def to_shard(
        self,
        destination: str | Path,
        *,
        shard_id: str,
        contract: Mapping[str, object],
        owner: int | None = None,
    ) -> dict[str, object]:
        """Write the collected rows. ``owner=None`` writes both perspectives."""

        owners = (0, 1) if owner is None else (owner,)
        records = [row for index in owners for row in self._rows[index]]
        frames = [frame for index in owners for frame in self._frames[index]]
        masks = [pair for index in owners for pair in self._row_masks[index]]
        if not records:
            raise ValueError("nothing to write: the recorder holds no closed rows")
        contract = dict(contract)
        from .event_rewards import reward_config
        configured = reward_config(self.reward_config)
        if "reward_config" in contract and contract["reward_config"] != configured:
            raise ValueError("shard reward configuration disagrees with recorder")
        if configured is not None:
            contract["reward_config"] = configured
        if contract.get("execution_credit_contract", EXECUTION_CREDIT_CONTRACT) != EXECUTION_CREDIT_CONTRACT:
            raise ValueError("shard execution credit contract mismatch")
        contract["execution_credit_contract"] = EXECUTION_CREDIT_CONTRACT
        if "action_constraints" in contract and contract["action_constraints"] != self.action_constraints:
            raise ValueError("shard contract disagrees with recorder action_constraints")
        if self.action_constraints is not None:
            contract["action_constraints"] = dict(self.action_constraints)
        for name in ("behavior_checkpoint_sha256", "behavior_model_contract_hash"):
            identity = getattr(self, name)
            if identity is not None:
                if name in contract and contract[name] != identity:
                    raise ValueError(f"shard contract disagrees with recorder {name}")
                contract[name] = identity
        return write_event_rl_shard(
            destination,
            records,
            frames,
            shard_id=shard_id,
            contract=contract,
            masks=masks,
        )


def training_segments(records, *, reward_config=None):
    """Return (source index, effective transition) for valid decisions only.

    Raw rows remain immutable audit data. Intervening excluded transitions are
    folded into the preceding effective decision, retaining elapsed ticks and
    the true terminal result. No excluded observation/value/probability enters
    GAE or any loss. Existing gamma/lambda remain per *effective decision*, not
    per tick; this does not introduce a new wall-clock discount convention.
    """
    if any(row.execution_status == "legacy_unknown" for row in records):
        raise ValueError("legacy rollout has no execution evidence; recollect before training")
    if not records:
        return []
    from .event_tower_rewards import validate_tower_rows, shape_segments
    validate_tower_rows(records, reward_config)
    if (not records[-1].done or any(row.done for row in records[:-1])
            or any(row.truncated for row in records)):
        raise ValueError("event training requires complete, untruncated episodes")
    kept = [i for i, row in enumerate(records) if row.trainable]
    result = []
    for i, stop in zip(kept, kept[1:] + [len(records)]):
        segment = records[i:stop]
        result.append((i, replace(records[i],
            decision_ticks=sum(row.decision_ticks for row in segment),
            reward=sum(row.reward for row in segment),
            base_reward=None if records[i].base_reward is None else sum(row.base_reward for row in segment),
            overflow_penalty=sum(row.overflow_penalty for row in segment),
            done=segment[-1].done, truncated=segment[-1].truncated)))
    return shape_segments(result, reward_config)


def lanes_from_shard(shard, *, owner: int | None = None) -> list:
    """Group a shard's rows into per-owner, per-match PPO lanes.

    Rows arrive in decision order (the recorder writes them that way) and both
    sides of a mirror match are separate lanes: one actor's decision sequence is
    what GAE walks, and slicing a longer timeline would let the opponent's rows
    commute the rewards.
    """

    from .event_ppo import EventLaneRolloutV1
    from .event_model import EventActionV1

    if (
        shard.card_masks is None
        or shard.cell_masks is None
        or shard.delay_masks is None
        or shard.gate_masks is None
    ):
        raise ValueError("this shard carries no legality masks, so a re-score is impossible")
    owners = (0, 1) if owner is None else (owner,)
    lanes = []
    for side in owners:
        for match_id in dict.fromkeys(row.match_id for row in shard.records if row.owner == side):
            indices = [
                index
                for index, row in enumerate(shard.records)
                if row.owner == side and row.match_id == match_id
            ]
            configured_rewards = shard.manifest.get("contract", {}).get("reward_config")
            segments = training_segments([shard.records[index] for index in indices],
                                         reward_config=configured_rewards)
            if not segments:
                continue
            indices = [indices[index] for index, _ in segments]
            rows = [row for _, row in segments]
            for source_index, row in zip(indices, rows):
                if row.ability_candidate >= 0:
                    candidates = shard.observation(source_index).candidates
                    i = row.ability_candidate
                    if (i >= candidates.mask.shape[1]
                            or int(candidates.native_source_entity[0, i]) != row.ability_source_entity
                            or int(candidates.ability_vocab_id[0, i]) != row.ability_vocab_id):
                        raise ValueError("ability record identity differs from its stored snapshot")
            lanes.append(
                EventLaneRolloutV1(
                    observations=tuple(shard.observation(index) for index in indices),
                    actions=EventActionV1(
                        act=torch.tensor([row.act for row in rows], dtype=torch.bool),
                        card_row=torch.tensor([row.card_row for row in rows], dtype=torch.long),
                        cell=torch.tensor([row.cell for row in rows], dtype=torch.long),
                        delay_bin=torch.tensor([row.delay_bin for row in rows], dtype=torch.long),
                        ability_candidate=torch.tensor([row.ability_candidate for row in rows], dtype=torch.long),
                    ),
                    old_log_prob=torch.tensor([row.log_prob for row in rows], dtype=torch.float32),
                    reward=torch.tensor([row.reward for row in rows], dtype=torch.float32),
                    reward_parts=torch.tensor([
                        [row.reward if row.base_reward is None else row.base_reward,
                         row.tower_shaping, -row.overflow_penalty] for row in rows], dtype=torch.float32),
                    terminal_reward=sum((r.reward if r.base_reward is None else r.base_reward)
                                        for r in shard.records if r.owner == side and r.match_id == match_id),
                    done=torch.tensor([row.done for row in rows], dtype=torch.bool),
                    truncated=torch.tensor([row.truncated for row in rows], dtype=torch.bool),
                    card_mask=shard.card_masks[indices],
                    cell_masks=shard.cell_masks[indices],
                    delay_mask=shard.delay_masks[indices],
                    gate_mask=shard.gate_masks[indices],
                    temperatures=tuple(rows[0].temperatures),
                    policy_version=rows[0].policy_version,
                    deck_id=shard.manifest.get("contract", {}).get("mixed_opponents", {}).get(
                        "owner_deck_ids", ["", ""])[side],
                    decision_ticks=torch.tensor([row.decision_ticks for row in rows], dtype=torch.long),
                    reward_config=configured_rewards,
                )
            )
    for lane in lanes:
        lane.validate()
    return lanes
