"""Capture real engine observations at public event boundaries.

This reuses the fixed-step IL replay machinery (calibrated deal, renderer
scheduling, resident batching, per-owner semantic tensorizers) but advances in
bulk between scheduled expert commands and lands exactly on each command tick,
where it tensorizes the event observation and pairs it with an expert
next-action label (card, cell, 100ms-quantized delay, or WAIT) built from the
parsed replay itself.  Labels therefore use model-internal card identities
instead of external card keys.

The public event set is the engine's own public deployment ring, inferred here
from the known command ticks.  Every capture tick is checked against the ring
and any disagreement is counted, so the cheap schedule-driven path stays
auditable against the P2 ring detection used at inference.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

import torch

from ..battle_env import BattleEnvError
from ..contracts import ActionKind, ActionV1
from .event_cache import EventObservationRecordV1
from .event_config import EventPolicyConfigV1
from .v4.config import ModelConfigV4
from .v4.expert import (
    FIRST_POLICY_DECISION_TICK,
    ExpertActionAlignmentError,
    TimedExpertActionV4,
    build_expert_action_batch,
)
from .v4.producer import (
    _ILReplayLaneV4,
    _build_il_lane,
    _initialize_il_lane,
    _wait_group,
)
from .v4.tensors import CANDIDATE_DEPLOY, UniversalSemanticBatchV4

if TYPE_CHECKING:
    from ...battle_env import BattleEnvV1
    from ...resident_batch_vector import ResidentBatchCoordinatorV1
    from ...royaleapi_replay import PreparedCollectedReplay


MAX_DELAY_MS = 6000
DELAY_STEP_MS = 100
EVENT_HORIZON_TICKS = MAX_DELAY_MS // 50


def quantize_delay_ms(ticks: int) -> int:
    """Round a tick separation to the 100ms event delay vocabulary."""

    if ticks < 0:
        raise ValueError("delay ticks must be non-negative")
    milliseconds = ticks * 50
    quantized = int(round(milliseconds / DELAY_STEP_MS) * DELAY_STEP_MS)
    return min(MAX_DELAY_MS, quantized)


@dataclass(frozen=True, slots=True)
class EventLabelV1:
    """The expert next-action target attached to one event observation."""

    owner: int
    trigger_tick: int
    wait: bool
    card_id: int | None
    hand_slot: int | None
    target_cell: tuple[int, int] | None
    delay_ms: int
    delay_bin: int
    target_tick: int | None


def build_event_label(
    owner_plays: Sequence[TimedExpertActionV4],
    *,
    owner: int,
    event_tick: int,
    config: EventPolicyConfigV1 | None = None,
) -> EventLabelV1:
    """Predict the owner's next card play after one event observation.

    The event observation is the first tick where the triggering deployment is
    visible (one tick after its recorded play tick), so the triggering play is
    never its own answer: the target is the next own play at or after this
    tick, with the delay measured from the observation.  A next play outside
    the six-second horizon is labelled WAIT, because a newer public event would
    cancel such a plan.  Targets remain in native engine coordinates here;
    record capture converts the cell once using the observation's perspective.
    """

    actual = config or EventPolicyConfigV1()
    following = next((item for item in owner_plays if item.source_tick >= event_tick), None)
    if following is None or (following.source_tick - event_tick) * 50 > actual.delay_offset_ms[-1]:
        return EventLabelV1(
            owner=owner,
            trigger_tick=event_tick,
            wait=True,
            card_id=None,
            hand_slot=None,
            target_cell=None,
            delay_ms=0,
            delay_bin=0,
            target_tick=None,
        )
    delay_ms = quantize_delay_ms(following.source_tick - event_tick)
    target_cell = following.action.target_grid
    if target_cell is None:
        return EventLabelV1(
            owner=owner,
            trigger_tick=event_tick,
            wait=True,
            card_id=None,
            hand_slot=None,
            target_cell=None,
            delay_ms=0,
            delay_bin=0,
            target_tick=None,
        )
    return EventLabelV1(
        owner=owner,
        trigger_tick=event_tick,
        wait=False,
        card_id=int(following.action.card_id) if following.action.card_id is not None else None,
        hand_slot=int(following.action.hand_slot) if following.action.hand_slot is not None else None,
        target_cell=(int(target_cell[0]), int(target_cell[1])),
        delay_ms=delay_ms,
        delay_bin=actual.delay_to_bin(delay_ms),
        target_tick=int(following.source_tick),
    )


@dataclass(frozen=True, slots=True)
class EventCandidateMatchV1:
    candidate_index: int
    candidate_uid: int
    legal_at_trigger: bool
    card_in_hand: bool


def resolve_event_candidate(batch: UniversalSemanticBatchV4, label: EventLabelV1) -> EventCandidateMatchV1:
    """Bind an ACT label to the observation's deploy candidates at trigger time."""

    if batch.batch_size != 1:
        raise ValueError("event candidate resolution needs a batch-size-one observation")
    if label.wait or label.card_id is None:
        return EventCandidateMatchV1(-1, -1, False, False)
    candidates = batch.candidates
    variant = candidates.variant[0]
    deploy = variant == CANDIDATE_DEPLOY
    visible = candidates.native_visible_card_id[0]
    card_in_hand = bool(torch.any(deploy & (visible == int(label.card_id))).item())
    legal = deploy & candidates.mask[0] & (visible == int(label.card_id))
    if label.hand_slot is not None:
        legal = legal & (candidates.native_hand_slot[0] == int(label.hand_slot))
    rows = legal.nonzero(as_tuple=False).flatten()
    if rows.numel() == 0:
        return EventCandidateMatchV1(-1, -1, False, card_in_hand)
    index = int(rows[0].item())
    return EventCandidateMatchV1(
        candidate_index=index,
        candidate_uid=int(candidates.uid[0, index].item()),
        legal_at_trigger=True,
        card_in_hand=card_in_hand,
    )


@dataclass(frozen=True, slots=True)
class EventReplayResultV1:
    replay_tag: str
    records: tuple[EventObservationRecordV1, ...]
    observations: tuple[UniversalSemanticBatchV4, ...]
    completed: bool
    failure_reason: str | None
    simulated_end_tick: int
    source_end_tick: int
    play_count: int
    captured_events: int
    ring_mismatches: int


@dataclass(slots=True)
class _EventLaneV1:
    lane: _ILReplayLaneV4
    plays_by_owner: tuple[tuple[TimedExpertActionV4, ...], tuple[TimedExpertActionV4, ...]]
    actions: tuple[TimedExpertActionV4, ...]
    checkpoints: tuple[int, ...]
    capture_ticks: tuple[int, ...]
    trigger_events: dict[int, tuple[tuple[int, TimedExpertActionV4], ...]]
    config: EventPolicyConfigV1
    cursor: int = 0
    captured_ticks: set[int] = field(default_factory=set)
    records: list[EventObservationRecordV1] = field(default_factory=list)
    observations: list[UniversalSemanticBatchV4] = field(default_factory=list)
    label_cache: dict[tuple[int, int], EventLabelV1] = field(default_factory=dict)
    ring_mismatches: int = 0
    primary_stop_flags: int = 0
    active: bool = True
    completed: bool = False
    failure_reason: str | None = None
    _reported_actions: set[int] = field(default_factory=set)
    frame_cache: dict[int, tuple[int, UniversalSemanticBatchV4]] = field(default_factory=dict)

    @property
    def current_tick(self) -> int:
        return int(self.lane.current_tick)

    @property
    def env_id(self) -> int:
        return int(self.lane.environment.native.env_id)

    @property
    def trigger_set(self) -> frozenset[int]:
        return frozenset(self.capture_ticks)

    def advance_cursor(self) -> None:
        current = self.current_tick
        while self.cursor < len(self.checkpoints) and self.checkpoints[self.cursor] <= current:
            self.cursor += 1

    def target_tick(self) -> int | None:
        """The next checkpoint to step onto; None once checkpoints run out.

        The post-battle tail is never entered because stepping there raises.
        """

        self.advance_cursor()
        if self.cursor < len(self.checkpoints):
            return self.checkpoints[self.cursor]
        return None

    def label(self, event_tick: int, owner: int) -> EventLabelV1:
        key = (owner, event_tick)
        cached = self.label_cache.get(key)
        if cached is not None:
            return cached
        built = build_event_label(
            self.plays_by_owner[owner], owner=owner, event_tick=event_tick, config=self.config
        )
        self.label_cache[key] = built
        return built

    def next_trigger_tick(self, event_tick: int) -> int | None:
        position = bisect_right(self.capture_ticks, event_tick)
        return self.capture_ticks[position] if position < len(self.capture_ticks) else None


def _build_event_lane(
    environment: BattleEnvV1, prepared: PreparedCollectedReplay, tensorizers: Sequence[Any]
) -> _EventLaneV1:
    lane = _build_il_lane(environment, prepared, tensorizers)
    actions = lane.expert_actions
    plays_by_owner = tuple(
        tuple(item for item in actions if item.owner == owner and item.action.kind is ActionKind.PLAY_CARD)
        for owner in (0, 1)
    )
    # The opening warmup lands directly on FIRST_POLICY_DECISION_TICK and the
    # renderer rejects commands at or before it, so only later ticks can be
    # landed on one tick early.  A play recorded at tick P is first visible at
    # observation tick P+1, which is the event observation.
    action_ticks = {
        int(item.source_tick)
        for item in actions
        if FIRST_POLICY_DECISION_TICK < int(item.source_tick) < lane.source_end_tick
    }
    collected: dict[int, list[tuple[int, TimedExpertActionV4]]] = {}
    for owner in (0, 1):
        for item in plays_by_owner[owner]:
            play_tick = int(item.source_tick)
            if not FIRST_POLICY_DECISION_TICK < play_tick < lane.source_end_tick:
                continue
            event_tick = play_tick + 1
            if event_tick >= lane.source_end_tick:
                continue
            collected.setdefault(event_tick, []).append((owner, item))
    trigger_events = {tick: tuple(entries) for tick, entries in collected.items()}
    # Land one tick before every command (to record the previous action while the
    # card is still in hand) and one tick after every play (to observe the
    # deployed entity).  The command tick itself needs no observation, which
    # removes one resident round per command.
    landings = {tick - 1 for tick in action_ticks}
    checkpoints = tuple(sorted(landings | set(trigger_events)))
    return _EventLaneV1(
        lane=lane,
        plays_by_owner=plays_by_owner,  # type: ignore[arg-type]
        actions=actions,
        checkpoints=checkpoints,
        capture_ticks=tuple(sorted(trigger_events)),
        trigger_events=trigger_events,
        config=EventPolicyConfigV1(),
    )


def _frame_for(event_lane: _EventLaneV1, owner: int) -> UniversalSemanticBatchV4:
    """Tensorize each (owner, tick) exactly once; tracker updates are not idempotent."""

    lane = event_lane.lane
    if lane.observations is None:
        raise RuntimeError("event lane has no current observation")
    observation = lane.observations[owner]
    tick = int(observation.tick)
    cached = event_lane.frame_cache.get(owner)
    if cached is not None and cached[0] == tick:
        return cached[1]
    batch = lane.tensorizers[owner].tensorize(observation, validate=False)
    event_lane.frame_cache[owner] = (tick, batch)
    return batch


def _record_imminent_actions(event_lane: _EventLaneV1) -> None:
    """Feed an expert command into previous-action memory one tick before execution."""

    lane = event_lane.lane
    if lane.observations is None:
        return
    current = event_lane.current_tick
    for index, timed in enumerate(event_lane.actions):
        if index in event_lane._reported_actions:
            continue
        if timed.source_tick != current + 1:
            continue
        event_lane._reported_actions.add(index)
        owner = timed.owner
        tensorizer = lane.tensorizers[owner]
        try:
            batch = _frame_for(event_lane, owner)
            sequence = build_expert_action_batch(
                batch,
                ((timed,),),
                decision_ticks_by_row=(current,),
                perspectives=(tensorizer.perspective,),
                config=ModelConfigV4(),
                validate=False,
            )
            tensorizer.record_action(sequence, batch, row=0, validate=False)
        except ExpertActionAlignmentError:
            continue


def _capture_trigger(event_lane: _EventLaneV1, event_tick: int) -> None:
    """Capture both owner views at one public event observation tick.

    A play recorded at tick P first appears in the observation and the public
    deployment ring at tick P+1, so ``event_tick`` is the play tick plus one.
    This is also the tick the ring-driven P2 runtime acts on, which keeps the
    training observation aligned with inference.
    """

    lane = event_lane.lane
    if lane.observations is None:
        raise RuntimeError("event lane captured an event without a current observation")
    triggers = event_lane.trigger_events.get(event_tick, ())
    if not triggers:
        return
    frames: dict[int, UniversalSemanticBatchV4] = {}
    matches: dict[int, EventCandidateMatchV1] = {}
    labels: dict[int, EventLabelV1] = {}
    model_cells: dict[int, tuple[int, int] | None] = {}
    for owner in (0, 1):
        label = event_lane.label(event_tick, owner)
        # Keep cached labels and candidate hand slots native. Only the persisted
        # target cell uses the same model perspective as its observation.
        model_cells[owner] = (
            lane.tensorizers[owner].perspective.action_to_model(
                ActionV1.play(owner, label.hand_slot, label.target_cell, card_id=label.card_id)
            ).target_grid
            if not label.wait else None
        )
        frame = _frame_for(event_lane, owner)
        labels[owner] = label
        frames[owner] = frame
        matches[owner] = resolve_event_candidate(frame, label)
    stored = {owner: frames[owner].to_storage("cpu", float_dtype=torch.float16) for owner in (0, 1)}
    next_trigger = event_lane.next_trigger_tick(event_tick)
    terminal = next_trigger is None
    for trigger_owner, item in triggers:
        for owner in (0, 1):
            label = labels[owner]
            match = matches[owner]
            event_lane.records.append(
                EventObservationRecordV1(
                    replay_tag=lane.prepared.replay_tag,
                    owner=owner,
                    observation_tick=event_tick,
                    trigger_event_id=f"{lane.prepared.replay_tag}:{item.source_index}:{trigger_owner}",
                    trigger_owner=trigger_owner,
                    trigger_card_id=int(item.action.card_id) if item.action.card_id is not None else None,
                    wait=label.wait,
                    card_id=label.card_id,
                    hand_slot=label.hand_slot,
                    target_cell=model_cells[owner],
                    delay_ms=label.delay_ms,
                    delay_bin=label.delay_bin,
                    target_tick=label.target_tick,
                    next_trigger_tick=next_trigger,
                    terminal=bool(terminal and label.wait),
                    candidate_index=match.candidate_index,
                    candidate_uid=match.candidate_uid,
                    legal_at_trigger=match.legal_at_trigger,
                    card_in_hand=match.card_in_hand,
                )
            )
            event_lane.observations.append(stored[owner])
    event_lane.captured_ticks.add(event_tick)


def _accept_event_step(event_lane: _EventLaneV1, step_result: tuple[Any, Any, Any, Any, Any]) -> None:
    lane = event_lane.lane
    next_observations, _reward, terminated, truncated, _info = step_result
    next_tick = int(next_observations[0].tick)
    if next_tick <= lane.current_tick:
        raise RuntimeError("event replay engine did not advance the timeline")
    lane.observations = next_observations
    lane.current_tick = next_tick
    terminated_flag = bool(terminated[0])
    truncated_flag = bool(truncated[0])
    if terminated_flag:
        lane.active = False
        lane.completed = True
    elif next_tick >= lane.source_end_tick:
        lane.active = False
        lane.completed = True
    elif truncated_flag:
        lane.active = False
        lane.completed = False
        lane.failure_reason = "battle truncated before the source timeline"


def _is_battle_finished(error: BaseException) -> bool:
    return isinstance(error, BattleEnvError) and "already finished" in str(error)


def _fail_lane(event_lane: _EventLaneV1, error: BaseException) -> None:
    event_lane.active = False
    event_lane.completed = False
    event_lane.failure_reason = f"{type(error).__name__}: {error}"


def _finalize_lane(event_lane: _EventLaneV1) -> EventReplayResultV1:
    lane = event_lane.lane
    for tensorizer in lane.tensorizers:
        tensorizer.end_episode()
    return EventReplayResultV1(
        replay_tag=lane.prepared.replay_tag,
        records=tuple(event_lane.records),
        observations=tuple(event_lane.observations),
        completed=event_lane.completed,
        failure_reason=event_lane.failure_reason,
        simulated_end_tick=lane.current_tick,
        source_end_tick=lane.source_end_tick,
        play_count=sum(len(plays) for plays in event_lane.plays_by_owner),
        captured_events=len({record.observation_tick for record in event_lane.records}),
        ring_mismatches=event_lane.ring_mismatches,
    )


def _round_delta(event_lane: _EventLaneV1) -> int:
    """Ticks to advance to reach the next checkpoint, or zero when finished."""

    target = event_lane.target_tick()
    if target is None:
        return 0
    return max(1, target - event_lane.current_tick)


def _native_accept_step(event_lane: _EventLaneV1, step_result: tuple[Any, Any, Any, Any, Any]) -> None:
    """Process one native step-until-event result and capture its event ticks."""

    lane = event_lane.lane
    next_observations, _reward, terminated, truncated, _info = step_result
    next_tick = int(next_observations[0].tick)
    if next_tick <= lane.current_tick:
        raise RuntimeError("native event step did not advance the timeline")
    lane.observations = next_observations
    lane.current_tick = next_tick
    entries = event_lane.trigger_events.get(next_tick, ())
    if entries:
        for owner, item in entries:
            action = item.action
            form_code = int(action.metadata.get("form_code") or 0)
            lane.tensorizers[owner].record_expert_previous_action(
                card_id=int(action.card_id),
                target_grid=action.target_grid,
                effective_card_id=action.metadata.get("effective_card_id"),
                form_code=form_code,
            )
        if next_tick not in event_lane.captured_ticks:
            _capture_trigger(event_lane, next_tick)
    elif event_lane.primary_stop_flags & 0x02:
        event_lane.ring_mismatches += 1
    if bool(terminated[0]):
        lane.active = False
        lane.completed = True
    elif bool(truncated[0]):
        lane.active = False
        lane.completed = False
        lane.failure_reason = "battle truncated before the source timeline"


def produce_event_observation_batch_native(
    environments: Sequence[BattleEnvV1],
    prepared_replays: Sequence[PreparedCollectedReplay],
    tensorizers_by_lane: Sequence[Sequence[Any]],
    *,
    batch_coordinator: ResidentBatchCoordinatorV1,
    max_advance_ticks: int = 300,
) -> tuple[EventReplayResultV1, ...]:
    """Capture event observations through the native step-until-event primitive.

    Each batch round asks the engine to advance every lane to its own next
    public deployment (or the tick budget), so one resident round trip serves
    every lane's next event instead of one round trip per checkpoint.  The
    deployed play is fed into the owning tensorizer's previous-action memory
    directly, because at the stop tick it has already left the hand.
    """

    from concurrent.futures import ThreadPoolExecutor

    lane_count = len(environments)
    if lane_count == 0:
        raise ValueError("native event batch must contain at least one lane")
    if len(prepared_replays) != lane_count or len(tensorizers_by_lane) != lane_count:
        raise ValueError("native event batch inputs must have equal lengths")
    if not getattr(batch_coordinator, "step_until_event", False):
        raise ValueError("native event batch requires a step-until-event coordinator")
    if max_advance_ticks <= 0:
        raise ValueError("max advance ticks must be positive")
    if lane_count != 1:
        # Lanes stop at their own events and therefore advance unequal tick
        # counts.  The probe's native engine state is process-global, and a
        # second lane sharing the engine inherits the first lane's terminal
        # result and stops early, so require one engine per replay.
        raise ValueError("native step-until-event capture supports one lane per engine")
    env_ids = tuple(int(environment.native.env_id) for environment in environments)
    if len(set(env_ids)) != lane_count:
        raise ValueError("native event batch requires unique resident env IDs")

    lanes: list[_EventLaneV1 | None] = [
        _build_event_lane(environments[index], prepared_replays[index], tensorizers_by_lane[index])
        for index in range(lane_count)
    ]
    finalized: list[tuple[int, EventReplayResultV1]] = []
    finalized_indices: set[int] = set()
    session_started = False
    executor: ThreadPoolExecutor | None = None

    def finalize(index: int, event_lane: _EventLaneV1) -> None:
        if index in finalized_indices:
            raise RuntimeError("native event lane was finalized twice")
        finalized_indices.add(index)
        finalized.append((index, _finalize_lane(event_lane)))

    def step_lane(lane: _ILReplayLaneV4, actions: dict[int, tuple[ActionV1, ...]]) -> tuple[Any, Any, Any, Any, Any]:
        try:
            return lane.environment.step(actions, record_trace=False)
        except BaseException as error:
            batch_coordinator.abort_round(error)
            raise

    try:
        for index, lane in enumerate(lanes):
            assert lane is not None
            try:
                _initialize_il_lane(lane.lane)
            except Exception as error:
                _fail_lane(lane, error)
                finalize(index, lane)
        executor = ThreadPoolExecutor(max_workers=lane_count, thread_name_prefix="event-native")

        while True:
            active = [(index, lane) for index, lane in enumerate(lanes) if lane is not None and lane.active]
            if not active:
                break
            round_env_ids = tuple(lane.env_id for _index, lane in active)
            if not session_started:
                batch_coordinator.start_session()
                session_started = True
            batch_coordinator.begin_round(round_env_ids)
            round_actions = {lane.env_id: _wait_group(max_advance_ticks) for _index, lane in active}
            futures = {
                executor.submit(step_lane, lane.lane, round_actions[lane.env_id]): (index, lane)
                for index, lane in active
            }
            results: dict[int, tuple[Any, Any, Any, Any, Any]] = {}
            errors: dict[int, BaseException] = {}
            for future, (index, lane) in futures.items():
                try:
                    results[lane.env_id] = future.result()
                except BaseException as error:
                    errors[lane.env_id] = error
            succeeded = batch_coordinator.round_succeeded
            finish_error: BaseException | None = None
            try:
                batch_coordinator.finish_round()
            except BaseException as error:
                finish_error = error
            if not succeeded or finish_error is not None:
                round_error = finish_error or next(iter(errors.values()), None)
                if round_error is None:
                    round_error = RuntimeError("native event round did not complete")
                if not isinstance(round_error, Exception):
                    raise round_error
                for index, lane in active:
                    if lane.active:
                        if _is_battle_finished(round_error):
                            lane.active = False
                            lane.completed = True
                        else:
                            _fail_lane(lane, round_error)
                continue
            for index, lane in active:
                error = errors.get(lane.env_id)
                if error is not None:
                    if not isinstance(error, Exception):
                        raise error
                    if _is_battle_finished(error):
                        lane.active = False
                        lane.completed = True
                    else:
                        _fail_lane(lane, error)
                    continue
                lane.primary_stop_flags = int(getattr(lane.lane.environment.native, "last_stop_flags", 0))
                try:
                    _native_accept_step(lane, results[lane.env_id])
                except Exception as error:
                    _fail_lane(lane, error)
            for index, lane in list(enumerate(lanes)):
                if lane is not None and not lane.active and index not in finalized_indices:
                    finalize(index, lane)
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
        if session_started:
            batch_coordinator.close_session()
        for index, lane in enumerate(lanes):
            if lane is None or index in finalized_indices:
                continue
            finalize(index, lane)

    return tuple(result for _index, result in sorted(finalized))


def produce_event_observation_batch(
    environments: Sequence[BattleEnvV1],
    prepared_replays: Sequence[PreparedCollectedReplay],
    tensorizers_by_lane: Sequence[Sequence[Any]],
    *,
    batch_coordinator: ResidentBatchCoordinatorV1,
    max_advance_ticks: int = 120,
) -> tuple[EventReplayResultV1, ...]:
    """Replay matches, landing on every scheduled command and capturing events.

    All resident lanes must advance by the same tick count, so a batch step is
    the smallest needed delta.  Use one lane per engine (``replays_per_shard``
    of one) to keep bulk advances large; extra lanes remain correct but slower.
    """

    from concurrent.futures import ThreadPoolExecutor

    lane_count = len(environments)
    if lane_count == 0:
        raise ValueError("event replay batch must contain at least one lane")
    if len(prepared_replays) != lane_count or len(tensorizers_by_lane) != lane_count:
        raise ValueError("event replay batch inputs must have equal lengths")
    if max_advance_ticks <= 0:
        raise ValueError("max advance ticks must be positive")
    env_ids = tuple(int(environment.native.env_id) for environment in environments)
    if len(set(env_ids)) != lane_count:
        raise ValueError("event replay batch requires unique resident env IDs")
    if any(getattr(environment.native, "coordinator", None) is not batch_coordinator for environment in environments):
        raise ValueError("event batch coordinator must own every native proxy")

    lanes: list[_EventLaneV1 | None] = [
        _build_event_lane(environments[index], prepared_replays[index], tensorizers_by_lane[index])
        for index in range(lane_count)
    ]
    finalized: list[tuple[int, EventReplayResultV1]] = []
    finalized_indices: set[int] = set()
    session_started = False
    executor: ThreadPoolExecutor | None = None

    def step_lane(lane: _ILReplayLaneV4, actions: dict[int, tuple[ActionV1, ...]]) -> tuple[Any, Any, Any, Any, Any]:
        try:
            return lane.environment.step(actions, record_trace=False)
        except BaseException as error:
            batch_coordinator.abort_round(error)
            raise

    def finalize(index: int, event_lane: _EventLaneV1) -> None:
        if index in finalized_indices:
            raise RuntimeError("event replay lane was finalized twice")
        finalized_indices.add(index)
        finalized.append((index, _finalize_lane(event_lane)))

    try:
        for index, lane in enumerate(lanes):
            assert lane is not None
            try:
                _initialize_il_lane(lane.lane)
            except Exception as error:
                _fail_lane(lane, error)
                finalize(index, lane)
        executor = ThreadPoolExecutor(max_workers=lane_count, thread_name_prefix="event-slot")

        while True:
            active = [(index, lane) for index, lane in enumerate(lanes) if lane is not None and lane.active]
            if not active:
                break
            for _index, lane in active:
                if not lane.active:
                    continue
                try:
                    lane.advance_cursor()
                    if lane.current_tick in lane.trigger_events and lane.current_tick not in lane.captured_ticks:
                        _capture_trigger(lane, lane.current_tick)
                    _record_imminent_actions(lane)
                except Exception as error:
                    _fail_lane(lane, error)
            round_lanes = [(index, lane) for index, lane in active if lane.active]
            deltas = [_round_delta(lane) for _index, lane in round_lanes]
            live = [(index, lane) for (index, lane), delta in zip(round_lanes, deltas, strict=True) if delta > 0]
            for index, lane in round_lanes:
                if _round_delta(lane) == 0:
                    lane.active = False
                    lane.completed = True
            if not live:
                for index, lane in active:
                    if index not in finalized_indices:
                        finalize(index, lane)
                continue
            delta = min(max(1, _round_delta(lane)) for _index, lane in live)
            delta = min(delta, max_advance_ticks)
            round_env_ids = tuple(lane.env_id for _index, lane in live)
            if not session_started:
                batch_coordinator.start_session()
                session_started = True
            batch_coordinator.begin_round(round_env_ids)
            round_actions = {lane.env_id: _wait_group(delta) for _index, lane in live}
            futures = {
                executor.submit(step_lane, lane.lane, round_actions[lane.env_id]): (index, lane)
                for index, lane in live
            }
            results: dict[int, tuple[Any, Any, Any, Any, Any]] = {}
            errors: dict[int, BaseException] = {}
            for future, (index, lane) in futures.items():
                try:
                    results[lane.env_id] = future.result()
                except BaseException as error:
                    errors[lane.env_id] = error
            succeeded = batch_coordinator.round_succeeded
            finish_error: BaseException | None = None
            try:
                batch_coordinator.finish_round()
            except BaseException as error:
                finish_error = error
            if not succeeded or finish_error is not None:
                round_error = finish_error or next(iter(errors.values()), None)
                if round_error is None:
                    round_error = RuntimeError("resident event round did not complete")
                if not isinstance(round_error, Exception):
                    raise round_error
                for index, lane in live:
                    if not lane.active:
                        continue
                    if _is_battle_finished(round_error):
                        lane.active = False
                        lane.completed = True
                    else:
                        _fail_lane(lane, round_error)
                continue
            for index, lane in live:
                error = errors.get(lane.env_id)
                if error is not None:
                    if not isinstance(error, Exception):
                        raise error
                    if _is_battle_finished(error):
                        lane.active = False
                        lane.completed = True
                    else:
                        _fail_lane(lane, error)
                    continue
                try:
                    _accept_event_step(lane, results[lane.env_id])
                except Exception as error:
                    _fail_lane(lane, error)
            for index, lane in list(enumerate(lanes)):
                if lane is not None and not lane.active and index not in finalized_indices:
                    finalize(index, lane)
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
        if session_started:
            batch_coordinator.close_session()
        for index, lane in enumerate(lanes):
            if lane is None or index in finalized_indices:
                continue
            finalize(index, lane)

    return tuple(result for _index, result in sorted(finalized))
