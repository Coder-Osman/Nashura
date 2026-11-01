"""Low-cost continuation observations from executed real-world PLAY reservations.

These are behaviour-cloning targets, never additional MCTS roots. Each group's
auxiliary loss is averaged so longer delays do not get a larger loss budget.
"""

from copy import copy, deepcopy
from dataclasses import asdict, dataclass, field

from ..event_cache import canonicalize_event_observation
from ..fair_final.rollout import RolloutSnapshot
from ..fair_search.contracts import pack, unpack
from .protocol import (DELAY_MS, ActionMode, HandSlot, HandState, PendingRefill,
    SSPIAction, SSPIInformation, SSPILegality, from_full_action)


TEMPORAL_SCHEMA = "sspi-executed-continuation.v1"
TEMPORAL_AUX_WEIGHT = 0.5
SAMPLE_INTERVAL_TICKS = 4  # 200 ms; native ticks are 50 ms.


def serialize_information(information):
    return dict(owner=information.owner, tick=information.tick,
        fair_sha256=information.sha256, frame=pack(information.frame),
        hand=asdict(information.hand), card_ids=information.legality.card_ids,
        plays=information.legality.plays.detach().cpu().clone(),
        wait=bool(information.legality.modes[ActionMode.WAIT]))


def deserialize_information(payload):
    raw = payload["hand"]
    hand = HandState(deck=tuple(tuple(item) for item in raw["deck"]),
        slots=tuple(HandSlot(**item) for item in raw["slots"]),
        known_next_card_id=raw["known_next_card_id"],
        cycle=tuple(tuple(item) for item in raw["cycle"]),
        pending_refills=tuple(PendingRefill(**item) for item in raw["pending_refills"]))
    legality = SSPILegality(payload["card_ids"], payload["plays"], wait=payload["wait"])
    information = SSPIInformation(payload["owner"], payload["tick"],
        unpack(payload["frame"]), hand, legality)
    if information.sha256 != payload["fair_sha256"]:
        raise ValueError("serialized SSPI FAIR digest changed")
    return information


def fork_tensorizer(tensorizer):
    """Share immutable catalogs, but isolate every state/cache changed by tensorize."""
    result = copy(tensorizer)
    for name in (*RolloutSnapshot.tensorizer_fields, "tracker"):
        setattr(result, name, deepcopy(getattr(tensorizer, name)))
    for name in RolloutSnapshot.tensorizer_caches:
        setattr(result, name, dict(getattr(tensorizer, name)))
    return result


def capture_pending_information(environment, observation, tensorizer, *, catalog):
    frame = canonicalize_event_observation(tensorizer.tensorize(observation, validate=True))
    player = next(item for item in environment.raw_observation["players"]
        if int(item["owner"]) == observation.owner)
    hand = HandState.from_native(player)
    legality = SSPILegality.from_native(environment, observation, frame, observation.owner,
        catalog=catalog, hand_state=hand, require_boundary=False)
    return SSPIInformation(observation.owner, observation.tick, frame, hand, legality)


@dataclass
class _PendingGroup:
    root: dict
    plan: object
    action: SSPIAction
    tensorizer: object
    samples: list = field(default_factory=list)


class TemporalAugmenter:
    """Observe existing actual ticks; retain samples only after execution evidence."""

    def __init__(self):
        self.pending = {}
        self.captured = self.retained = self.discarded = 0

    def register(self, root, plan, tensorizer):
        action = from_full_action(plan.action)
        if action.mode != ActionMode.PLAY:
            return
        if root["owner"] != plan.owner or root["tick"] != plan.created_tick:
            raise ValueError("continuation source differs from its committed reservation")
        root.update(selected_action=action.key, reservation=plan.identity,
            due_tick=plan.due_tick, continuations=[])
        if plan.due_tick - plan.created_tick <= SAMPLE_INTERVAL_TICKS:
            return
        if plan.identity in self.pending:
            raise ValueError("duplicate continuation reservation")
        self.pending[plan.identity] = _PendingGroup(root, plan, action, fork_tensorizer(tensorizer))

    def observe(self, *, environment, observations, clock, evidence, catalog):
        # Settle before sampling: an executed/preempted/terminal frame is not an
        # intermediate state of the old reservation, even at a 200-ms boundary.
        for event in evidence:
            group = self.pending.pop(event["reservation"], None)
            if group is None:
                continue
            if event["owner"] != group.plan.owner:
                raise ValueError("continuation settlement owner changed")
            if event["reason"] == "executed":
                if event["tick"] != group.plan.due_tick:
                    raise ValueError("continuation executed at an unexpected tick")
                group.root["continuations"] = group.samples
                group.root["continuation_execution"] = dict(event)
                self.retained += len(group.samples)
            else:
                self.discarded += len(group.samples)
        for group in self.pending.values():
            plan = group.plan
            if clock.pending.get(plan.owner) != plan:
                raise ValueError("continuation lost its reservation without settlement evidence")
            elapsed = clock.tick - plan.created_tick
            if elapsed <= 0 or elapsed % SAMPLE_INTERVAL_TICKS:
                continue
            remaining_ms = (plan.due_tick - clock.tick) * 50
            if remaining_ms not in DELAY_MS:
                raise ValueError("continuation remaining delay is outside the action bins")
            action = SSPIAction.play(group.action.card_id, group.action.cell,
                DELAY_MS.index(remaining_ms))
            information = capture_pending_information(environment, observations[plan.owner],
                group.tensorizer, catalog=catalog)
            if information.tick != clock.tick or information.owner != plan.owner:
                raise ValueError("continuation observation tick/owner changed")
            if not information.legality.legal(action):
                continue  # Preserve genuine legality; never force a target into the mask.
            group.samples.append(dict(serialize_information(information),
                schema=TEMPORAL_SCHEMA, source_root_id=group.root["root_id"],
                reservation=plan.identity, source_tick=plan.created_tick,
                due_tick=plan.due_tick, action=action.key))
            self.captured += 1

    def statistics(self):
        return dict(schema=TEMPORAL_SCHEMA, interval_ticks=SAMPLE_INTERVAL_TICKS,
            source_weight_with_samples=1. - TEMPORAL_AUX_WEIGHT,
            auxiliary_group_weight=TEMPORAL_AUX_WEIGHT, captured=self.captured,
            retained=self.retained, discarded=self.discarded)
