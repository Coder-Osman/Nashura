from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import EventV1
from native_runner.tests.test_fair_final_history import opening_fixture, WaitOpponent
from native_runner.tests.test_fair_final_rollout import rollout
from native_runner.training.fair_final import history_belief, rollout as rollout_module
from native_runner.training.fair_final.history import FairHistory, FairHistoryRecorder, OwnDecision, OwnRejection, PublicDeployment
from native_runner.training.fair_final.history_belief import HistoryReplaySession, IncompatibleHistory
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.conditioned_plan import HistoryConditionedOpponent
from native_runner.training.fair_final.opening_belief import OpeningEvidence
from native_runner.training.fair_final.policy import FullAction, JointPolicy, LegalPrefixes
from native_runner.training.fair_final.timing import Reservation
from native_runner.training.fair_search.config import HOG_DECK


def rejected_event(*, tick=102, owner=0, **data):
    return EventV1(tick=tick, event_type="action_rejected", owner=owner, card_id=HOG_DECK[0],
        data={"private_to": owner, **data})


def recorder_fixture():
    observation, initial = opening_fixture()
    recorder = FairHistoryRecorder(observation, initial, opening_tick=100)
    recorder.record_decision(initial, FullAction(HOG_DECK[0], 0, 1))
    recorder.observe_tick(replace(observation, tick=101))
    return recorder, observation, initial


def test_rejection_projection_is_minimal_and_schema_isolated():
    recorder, observation, initial = recorder_fixture()
    event = rejected_event(action_id="native-private-id", reason="diagnostic", seed=123)
    recorder.observe_tick(replace(observation, tick=102, events=(event,)))
    history = recorder.capture(replace(initial, tick=102))
    assert history.rejections == (OwnRejection(102, 100),)
    payload = history.to_payload()
    assert payload["rejections"] == ({"tick": 102, "decision_tick": 100},)
    assert FairHistory.from_payload(payload) == history
    assert not hasattr(history.rejections[0], "action_id")
    with pytest.raises(ValueError, match="unknown fields"):
        FairHistory.from_payload({**payload, "schema": "fair-final-visible-history.v1"})
    with pytest.raises(ValueError, match="invalid record"):
        FairHistory.from_payload({**payload, "rejections": [{"tick": 102, "decision_tick": 100, "seed": 123}]})


@pytest.mark.parametrize("alteration", ["public", "other-owner", "wrong-card", "wrong-tick", "wait", "bool-private"])
def test_rejection_attestation_rejects_wrong_identity(alteration):
    decision = OwnDecision(100, "a" * 64, FullAction(HOG_DECK[0], 0, 1))
    event = rejected_event()
    if alteration == "public":
        event = replace(event, data={})
    elif alteration == "other-owner":
        event = rejected_event(owner=1)
    elif alteration == "wrong-card":
        event = replace(event, card_id=HOG_DECK[1])
    elif alteration == "wrong-tick":
        event = replace(event, tick=104)
    elif alteration == "wait":
        decision = replace(decision, action=FullAction())
    else:
        event = rejected_event(private_to=False)
    with pytest.raises(ValueError, match="due ACT"):
        OwnRejection.from_event(event, owner=0, decision=decision)


@pytest.mark.parametrize("tick,decision_tick", [(101, 100), (100, 100), (162, 100), (102, True)])
def test_rejection_timing_is_exact(tick, decision_tick):
    with pytest.raises(ValueError, match="timing"):
        OwnRejection(tick, decision_tick)


def test_rejection_window_deduplicates_after_same_tick_new_decision():
    recorder, observation, initial = recorder_fixture()
    event = rejected_event()
    recorder.observe_tick(replace(observation, tick=102, events=(event,)))
    middle = replace(initial, tick=102)
    first = recorder.capture(middle)
    recorder.record_decision(middle, FullAction())
    recorder.observe_tick(replace(observation, tick=103, events=(event, event)))
    later = recorder.capture(replace(initial, tick=103))
    assert later.rejections == first.rejections and later.extends(first)


def test_rejection_recorder_is_atomic_and_rejects_late_evidence():
    recorder, observation, initial = recorder_fixture()
    event = rejected_event()
    future = replace(event, tick=104)
    with pytest.raises(ValueError):
        recorder.observe_tick(replace(observation, tick=102, events=(event, future)))
    assert recorder.observed_tick == 101 and recorder.rejections == [] and not recorder.seen_rejections
    recorder.observe_tick(replace(observation, tick=102))
    with pytest.raises(ValueError, match="history boundary"):
        recorder.observe_tick(replace(observation, tick=103, events=(event,)))
    assert recorder.observed_tick == 102 and recorder.rejections == []


def test_opponent_private_rejection_never_enters_own_history():
    recorder, observation, initial = recorder_fixture()
    recorder.observe_tick(replace(observation, tick=102, events=(rejected_event(owner=1),)))
    assert recorder.capture(replace(initial, tick=102)).rejections == ()


def test_rejection_history_rejects_preemption_and_missing_decision():
    recorder, observation, initial = recorder_fixture()
    recorder.observe_tick(replace(observation, tick=102, events=(rejected_event(),)))
    history = recorder.capture(replace(initial, tick=102))
    with pytest.raises(ValueError, match="unpreempted"):
        replace(history, rejections=(OwnRejection(104, 102),), root_tick=104)
    longer = replace(history.decisions[0], action=FullAction(HOG_DECK[0], 0, 2))
    with pytest.raises(ValueError, match="unpreempted"):
        replace(history, root_tick=104, decisions=(longer, OwnDecision(102, "b" * 64, FullAction())),
            rejections=(OwnRejection(104, 100),))


def test_rejection_on_opponent_public_execution_tick_is_consistent():
    recorder, observation, initial = recorder_fixture()
    event = EventV1(tick=102, event_type="action_executed", owner=1, card_id=HOG_DECK[1],
        position=(1500., 2500.))
    recorder.observe_tick(replace(observation, tick=102, events=(event, rejected_event())))
    history = recorder.capture(replace(initial, tick=102))
    assert history.rejections == (OwnRejection(102, 100),)
    assert history.deployments == (PublicDeployment(102, 1, HOG_DECK[1], 1500, 2500),)
    with pytest.raises(ValueError, match="unpreempted"):
        replace(history, deployments=(replace(history.deployments[0], owner=0),))
    recorder.record_decision(replace(initial, tick=102), FullAction())
    recorder.observe_tick(replace(observation, tick=103, events=(event, rejected_event())))
    next_history = recorder.capture(replace(initial, tick=103))
    assert next_history.extends(history)
    assert not replace(next_history, rejections=()).extends(history)


def install_rejection_environment(rollout, monkeypatch, *, rejected=(0,), missing_evidence=False):
    monkeypatch.setattr(history_belief, "validate_fair_view", lambda *args: None)
    monkeypatch.setattr(rollout_module, "legal_prefixes", lambda *args, **kwargs:
        LegalPrefixes([HOG_DECK[0]], torch.ones(1, 1, 31, dtype=torch.bool)))
    environment = rollout.runtime.environment
    environment.rejected = set(rejected)
    environment.latest_events = ()
    advance = environment.advance_reservations

    def advance_with_evidence(plans):
        outcomes, result = advance(plans)
        environment.latest_events = tuple(rejected_event(tick=environment.current_tick, owner=plan.owner)
            for plan in plans if outcomes[plan.identity] == "rejected" and not missing_evidence)
        return outcomes, result

    environment.advance_reservations = advance_with_evidence
    environment.action_outcomes = lambda: environment.latest_events
    initial = rollout.freeze_due_information()[0]
    return initial, OpeningEvidence(initial.sha256, 0, 100, HOG_DECK)


def policy_for_action(action):
    plays = torch.zeros(1, 1, 31, dtype=torch.bool)
    if action.card_id is not None:
        plays[0, 0, action.delay_slot] = True
    return JointPolicy(lambda prefix: torch.zeros((2, 1, 1, 31)[len(prefix)]),
        LegalPrefixes([HOG_DECK[0]], plays, wait=action.card_id is None))


def test_own_rejection_replays_cold_incrementally_and_rolls_back(rollout, monkeypatch):
    initial, opening = install_rejection_environment(rollout, monkeypatch)
    with rollout.snapshot(None) as start:
        rollout.commit_simultaneous({0: FullAction(HOG_DECK[0], 0, 1), 1: FullAction()})
        rollout.advance()
        rollout.advance()
        middle = rollout.freeze_due_information()[0]
        assert set(rollout.runtime.clock.pending) == {1}
        rollout.commit_simultaneous({0: FullAction()})
        for step in range(60):
            if rollout.runtime.clock.ready:
                rollout.commit_simultaneous({owner: FullAction() for owner in rollout.runtime.clock.ready})
            rollout.advance()
        end = rollout.freeze_due_information()[0]
        first = FairHistory(opening, (OwnDecision(100, initial.sha256, FullAction(HOG_DECK[0], 0, 1)),),
            (), 102, middle.sha256, (OwnRejection(102, 100),))
        extended = replace(first, decisions=(*first.decisions, OwnDecision(102, middle.sha256, FullAction())),
            root_tick=162, root_fair_sha=end.sha256)
        start.restore()
        session = HistoryReplaySession(rollout, opening=opening, opponent=WaitOpponent(), seed=17,
            sidecar_factory=None)
        with pytest.raises(IncompatibleHistory, match="visible history"):
            session.sync(replace(first, rejections=()), middle)
        assert session.history.root_tick == 100 and rollout.runtime.clock.tick == 100
        assert session.sync(first, middle).sha256 == middle.sha256
        assert set(rollout.runtime.clock.pending) == {1}
        assert session.sync(extended, end).sha256 == end.sha256
        assert session.profile["incremental_ticks"] == 60
        session.close()
        start.restore()
        cold = HistoryReplaySession(rollout, opening=opening, opponent=WaitOpponent(), seed=17,
            sidecar_factory=None)
        assert cold.sync(extended, end).sha256 == end.sha256
        cold.close()


def test_missing_native_rejection_evidence_is_protocol_error_and_restores(rollout, monkeypatch):
    initial, opening = install_rejection_environment(rollout, monkeypatch, missing_evidence=True)
    target = replace(initial, tick=102)
    history = FairHistory(opening, (OwnDecision(100, initial.sha256, FullAction(HOG_DECK[0], 0, 1)),),
        (), 102, target.sha256, (OwnRejection(102, 100),))
    session = HistoryReplaySession(rollout, opening=opening, opponent=WaitOpponent(), seed=17,
        sidecar_factory=None)
    with pytest.raises(ValueError, match="complete private execution evidence") as failure:
        session.sync(history, target)
    assert not isinstance(failure.value, IncompatibleHistory)
    assert session.history.root_tick == rollout.runtime.clock.tick == 100
    assert session.profile["syncs"] == 0
    session.close()


@pytest.mark.parametrize("conditioned", [False, True])
def test_rejection_checkpoint_failure_restores_previous_root(rollout, monkeypatch, conditioned):
    initial, opening = install_rejection_environment(rollout, monkeypatch)
    with rollout.snapshot(None) as start:
        rollout.commit_simultaneous({0: FullAction(HOG_DECK[0], 0, 1), 1: FullAction()})
        rollout.advance()
        rollout.advance()
        target = rollout.freeze_due_information()[0]
        start.restore()
        history = FairHistory(opening, (OwnDecision(100, initial.sha256, FullAction(HOG_DECK[0], 0, 1)),),
            (), 102, target.sha256, (OwnRejection(102, 100),))
        opponent = HistoryConditionedOpponent(SimpleNamespace(predict=lambda info:
            (policy_for_action(FullAction()), None)), history) if conditioned else WaitOpponent()
        session = HistoryReplaySession(rollout, opening=opening, opponent=opponent, seed=17,
            sidecar_factory=None)
        previous = session.root
        profile = dict(opponent.profile) if conditioned else None

        def fail_snapshot(factory):
            raise RuntimeError("replacement checkpoint failed")

        with monkeypatch.context() as patch:
            patch.setattr(rollout, "snapshot", fail_snapshot)
            with pytest.raises(RuntimeError, match="replacement checkpoint failed"):
                session.sync(history, target)
        assert session.root is previous and not previous.native.closed
        assert session.history.root_tick == rollout.runtime.clock.tick == 100
        assert session.profile["syncs"] == 0
        if conditioned:
            assert opponent.profile == profile
        session.sync(history, target)
        assert previous.native.closed
        session.close()


@pytest.mark.parametrize("conditioned", [False, True])
def test_independently_generated_opponent_rejection_preserves_owner_pending(rollout, monkeypatch, conditioned):
    initial, opening = install_rejection_environment(rollout, monkeypatch, rejected=(0, 1))
    opponent = SimpleNamespace(sample=lambda info, generator:
        FullAction(HOG_DECK[0], 0, 2) if info.tick == 100 else FullAction())
    with rollout.snapshot(None) as start:
        rollout.commit_simultaneous({0: FullAction(HOG_DECK[0], 0, 1), 1: FullAction(HOG_DECK[0], 0, 2)})
        for step in range(2):
            rollout.advance()
        middle = rollout.freeze_due_information()[0]
        rollout.commit_simultaneous({0: FullAction()})
        for step in range(60):
            if rollout.runtime.clock.ready:
                rollout.commit_simultaneous({owner: FullAction() for owner in rollout.runtime.clock.ready})
            rollout.advance()
        end = rollout.freeze_due_information()[0]
        first = FairHistory(opening, (OwnDecision(100, initial.sha256, FullAction(HOG_DECK[0], 0, 1)),),
            (), 102, middle.sha256, (OwnRejection(102, 100),))
        extended = replace(first, decisions=(*first.decisions, OwnDecision(102, middle.sha256, FullAction())),
            root_tick=162, root_fair_sha=end.sha256)
        seed = 17
        if conditioned:
            def predict(info):
                return policy_for_action(FullAction(HOG_DECK[0], 0, 2) if info.tick == 100 else FullAction()), None
            opponent = HistoryConditionedOpponent(SimpleNamespace(predict=predict), extended)
        start.restore()
        session = HistoryReplaySession(rollout, opening=opening, opponent=opponent, seed=seed,
            sidecar_factory=None)
        session.sync(first, middle)
        session.sync(extended, end)
        assert [entry["owner"] for entry in rollout.runtime.clock.evidence if entry["reason"] == "rejected"] == [0, 1]
        assert session.history.rejections == (OwnRejection(102, 100),)
        session.close()
        start.restore()
        cold = HistoryReplaySession(rollout, opening=opening, opponent=opponent, seed=seed,
            sidecar_factory=None)
        assert cold.sync(extended, end).sha256 == end.sha256
        assert [entry["owner"] for entry in rollout.runtime.clock.evidence if entry["reason"] == "rejected"] == [0, 1]
        assert cold.history.rejections == (OwnRejection(102, 100),)
        cold.close()


@pytest.mark.parametrize("conditioned", [False, True])
def test_hidden_rejection_can_open_new_reservation_before_later_public_play(rollout, monkeypatch, conditioned):
    initial, opening = install_rejection_environment(rollout, monkeypatch, rejected=(1,))
    environment = rollout.runtime.environment
    advance = environment.advance_reservations
    def scheduled_advance(plans):
        environment.rejected = {1} if environment.current_tick + 1 == 104 else set()
        outcomes, result = advance(plans)
        environment.latest_events += tuple(EventV1(tick=environment.current_tick,
            event_type="action_executed", owner=plan.owner, card_id=plan.action.card_id,
            position=(1500., 2500.)) for plan in plans if outcomes[plan.identity] == "executed")
        return outcomes, result
    environment.advance_reservations = scheduled_advance
    def action(info):
        return FullAction(HOG_DECK[0], 0, 2 if info.tick == 100 else 1)
    with rollout.snapshot(None) as start:
        rollout.commit_simultaneous({0: FullAction(), 1: FullAction(HOG_DECK[0], 0, 2)})
        for step in range(6):
            if rollout.runtime.clock.ready:
                rollout.commit_simultaneous({owner: action(SimpleNamespace(tick=environment.current_tick))
                    for owner in rollout.runtime.clock.ready})
            rollout.advance()
        target = rollout.freeze_due_information()[0]
        history = FairHistory(opening, (OwnDecision(100, initial.sha256, FullAction()),),
            (PublicDeployment(106, 1, HOG_DECK[0], 1500, 2500),), 106, target.sha256)
        start.restore()
        monkeypatch.setattr(history_belief, "deployment_action", lambda replay, info, event, **kwargs:
            action(info) if info.tick == 104 else None)
        opponent = HistoryConditionedOpponent(SimpleNamespace(predict=lambda info:
            (policy_for_action(action(info)), None)), history) if conditioned else SimpleNamespace(
                sample=lambda info, generator: action(info))
        session = HistoryReplaySession(rollout, opening=opening, opponent=opponent, seed=17,
            sidecar_factory=None)
        assert session.sync(history, target).sha256 == target.sha256
        assert [(entry["tick"], entry["reason"]) for entry in rollout.runtime.clock.evidence
            if entry["owner"] == 1] == [(104, "rejected"), (106, "executed")]
        assert session.history.rejections == ()
        session.close()


def test_missing_reserved_card_emits_private_evidence_after_step():
    environment = object.__new__(FinalBattleEnv)
    environment._raw = {"tick": 100, "players": [{"owner": 0, "hand": []}]}
    environment._canonical_tick = lambda tick: tick
    environment._pending_abilities = []
    environment._pending = []
    events = []
    environment.reset_decision_deadlines = lambda: None

    def step(actions, **kwargs):
        assert not events
        environment._raw["tick"] = 101
        return {}, {}, {"__all__": False}, {"__all__": False}, {}

    environment.step = step
    environment._emit_event = lambda **fields: events.append(EventV1(**fields))
    plan = Reservation(7, 0, FullAction(HOG_DECK[0], 0, 1), 99, 101)
    outcomes, result = environment.advance_reservations((plan,))
    assert outcomes == {7: "rejected"}
    decision = OwnDecision(99, "a" * 64, plan.action)
    assert OwnRejection.from_event(events[0], owner=0, decision=decision) == OwnRejection(101, 99)
    assert events[0].data["private_to"] == 0
