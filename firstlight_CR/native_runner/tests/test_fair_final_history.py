from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
import torch

from native_runner.battle_env import BattleEnvV1
from native_runner.contracts import EventV1, frozen_mapping
from native_runner.tests.test_fair_final_protocol import toy_policy
from native_runner.tests.test_fair_final_rollout import rollout
from native_runner.tests.test_training_tracking import _observation
from native_runner.training.fair_final import history_belief
from native_runner.training.fair_final.history import FairHistory, FairHistoryRecorder, OwnDecision, PublicDeployment
from native_runner.training.fair_final.history_belief import HistoryBeliefFactory, HistoryReplaySession, IncompatibleHistory, compatible_opponent_draw
from native_runner.training.fair_final.opening_belief import OpeningEvidence
from native_runner.training.fair_final.policy import FullAction, LegalPrefixes
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_search.belief import public_draw_compatible
from native_runner.training.fair_search.config import HOG_DECK


def opening_fixture():
    observation = _observation(tick=100)
    own = replace(observation.players[0], hand=HOG_DECK[:4], cycle=HOG_DECK[4:], deck=HOG_DECK,
        next_card=HOG_DECK[4], metadata=frozen_mapping({"hand_slot_by_card":
            {str(card): slot for slot, card in enumerate(HOG_DECK[:4])}}))
    observation = replace(observation, players=(own, observation.players[1]))
    policy, _ = toy_policy()
    information = DecisionInformation(0, 100, {"own": True}, policy.legality)
    return observation, information


def history_fixture():
    observation, information = opening_fixture()
    opening = OpeningEvidence.from_view(observation, information, opening_tick=100)
    return FairHistory(opening, (), (), 100, information.sha256)


def test_strict_history_roundtrip_and_no_private_audit_fields():
    history = history_fixture()
    payload = json.loads(json.dumps(history.to_payload()))
    assert FairHistory.from_payload(payload) == history
    assert FairHistory.from_payload(payload).sha256 == history.sha256
    for key in ("seed", "opponent_hand", "native_digest", "pending_enemy_plan"):
        with pytest.raises(ValueError, match="private audit"):
            FairHistory.from_payload({**payload, key: 17})
    payload["opening"]["seed"] = 17
    with pytest.raises(ValueError, match="invalid record"):
        FairHistory.from_payload(payload)


def test_history_extension_binds_previous_root_and_prefix():
    first = history_fixture()
    decision = OwnDecision(100, first.root_fair_sha, FullAction())
    second = replace(first, decisions=(decision,), root_tick=160, root_fair_sha="b" * 64)
    assert second.extends(first)
    altered = replace(second, root_fair_sha="c" * 64)
    assert not altered.extends(second)
    third = replace(second, root_tick=220, root_fair_sha="d" * 64,
        decisions=(decision, OwnDecision(160, "e" * 64, FullAction())))
    assert not third.extends(second)
    with pytest.raises(ValueError, match="incomplete"):
        replace(second, decisions=())
    with pytest.raises(ValueError, match="order"):
        replace(second, decisions=(decision, decision))
    injected = replace(second, deployments=(PublicDeployment(120, 1, HOG_DECK[0], 1500, 2500),))
    assert not injected.extends(second)
    injected_later = replace(third, decisions=(*second.decisions, OwnDecision(160, second.root_fair_sha, FullAction())),
        deployments=injected.deployments)
    assert not injected_later.extends(second)
    assert not second.extends(None)


@pytest.mark.parametrize("data", [{"private_to": 1}, {"private_to": 0}, {"oracle_only": True}])
def test_private_event_never_becomes_public_history(data):
    event = EventV1(tick=102, event_type="action_executed", owner=1, card_id=HOG_DECK[0],
        position=(1500., 2500.), data=data)
    with pytest.raises(ValueError, match="publicly"):
        PublicDeployment.from_event(event)


def test_public_projection_drops_action_identity_and_private_metadata():
    event = EventV1(tick=102, event_type="action_executed", owner=1, card_id=HOG_DECK[0],
        position=(1500., 2500.), data={"action_id": "private", "requested_tick": 100, "seed": 123})
    projected = PublicDeployment.from_event(event)
    assert projected == PublicDeployment(102, 1, HOG_DECK[0], 1500, 2500)
    assert not hasattr(projected, "requested_tick")
    with pytest.raises(ValueError, match="integers"):
        PublicDeployment.from_event(replace(event, position=(1500.1, 2500.)))


def test_recorder_requires_contiguous_ticks_and_deduplicates_window():
    observation, information = opening_fixture()
    recorder = FairHistoryRecorder(observation, information, opening_tick=100)
    assert recorder.capture(information) == history_fixture()
    recorder.record_decision(information, FullAction())
    with pytest.raises(ValueError, match="every tick"):
        recorder.observe_tick(replace(observation, tick=102))
    event = EventV1(tick=101, event_type="action_executed", owner=1, card_id=HOG_DECK[0], position=(1500., 2500.))
    recorder.observe_tick(replace(observation, tick=101, events=(event,)))
    recorder.observe_tick(replace(observation, tick=102, events=(event,)))
    current = replace(information, tick=102)
    captured = recorder.capture(current)
    assert len(captured.deployments) == 1 and captured.extends(history_fixture())
    with pytest.raises(ValueError, match="future"):
        recorder.observe_tick(replace(observation, tick=103, events=(replace(event, tick=104),)))
    assert recorder.observed_tick == 102
    with pytest.raises(ValueError, match="boundary"):
        recorder.record_decision(replace(current, owner=1), FullAction())


def test_public_cycle_sampling_is_deterministic_and_compatible():
    opening = history_fixture()
    sequence = (*HOG_DECK, *HOG_DECK)
    events = tuple(PublicDeployment(102 + index * 60, 1, card, 1500, 2500)
        for index, card in enumerate(sequence))
    history = replace(opening, decisions=(OwnDecision(100, opening.root_fair_sha, FullAction()),),
        deployments=events, root_tick=1100, root_fair_sha="f" * 64)
    first = compatible_opponent_draw(history, 77)
    assert first == compatible_opponent_draw(history, 77)
    assert public_draw_compatible(first, sequence)
    impossible = replace(history, deployments=(events[0], replace(events[0], tick=162)))
    with pytest.raises(IncompatibleHistory, match="no fixed-deck"):
        compatible_opponent_draw(impossible, 77)


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("offset", [None, (.25, -.25)])
def test_public_deployment_reconstructs_exact_perspective_and_legal_delay(owner, offset):
    observation, _ = opening_fixture()
    own, enemy = observation.players
    observation = replace(observation, owner=owner, players=(replace(own, owner=owner), replace(enemy, owner=1-owner)))
    card = HOG_DECK[0]
    plays = torch.zeros(1, 576, 31, dtype=torch.bool)
    plays[0, 20, 3] = True
    legality = LegalPrefixes([card], plays)
    info = DecisionInformation(owner, 100, {}, legality)
    environment = SimpleNamespace(action_mask=lambda side: SimpleNamespace(
        placement_masks={"0": {"model_subcell_offset": offset}}))
    environment._world_target = lambda action: BattleEnvV1._world_target(environment, action)
    replay = SimpleNamespace(runtime=SimpleNamespace(environment=environment, frozen={owner: observation}))
    grid = (2, 1) if owner == 0 else (15, 30)
    sign = 1 if owner == 0 else -1
    x = grid[0]*1000+500+(round(sign*offset[0]*1000) if offset else 0)
    y = grid[1]*1000+500+(round(sign*offset[1]*1000) if offset else 0)
    event = PublicDeployment(106, owner, card, x, y)
    assert history_belief.deployment_action(replay, info, event) == FullAction(card, 20, 3)
    assert history_belief.deployment_action(replay, info, replace(event, tick=105)) is None
    assert history_belief.deployment_action(replay, info, replace(event, tick=102)) is None
    assert history_belief.deployment_action(replay, info, replace(event, x=x+1)) is None
    assert history_belief.deployment_action(replay, info, replace(event, owner=1-owner)) is None


class WaitOpponent:
    def __init__(self):
        self.owners = []

    def sample(self, information, generator):
        self.owners.append(information.owner)
        return FullAction()


def test_hidden_plan_due_on_public_tick_is_not_misread_as_preempted():
    session = object.__new__(HistoryReplaySession)
    session.seed, session.maximum_plan_attempts = 17, 3
    sampled = iter((FullAction(11, 0, 2), FullAction(11, 0, 3)))
    session.opponent = SimpleNamespace(sample=lambda info, generator: next(sampled))
    info = SimpleNamespace(tick=100, owner=1, legality=SimpleNamespace(legal=lambda action: True))
    history = SimpleNamespace(root_tick=110, deployments=(PublicDeployment(104, 0, HOG_DECK[0], 1500, 2500),))
    assert session._opponent_action(info, history) == FullAction(11, 0, 3)


def test_hidden_plan_sampling_deadline_and_budget_are_bounded():
    session = object.__new__(HistoryReplaySession)
    session.seed, session.maximum_plan_attempts = 17, 3
    calls = []
    def sample(info, generator):
        calls.append(info.owner)
        return FullAction(11, 0, 1)
    session.opponent = SimpleNamespace(sample=sample)
    info = SimpleNamespace(tick=100, owner=1, legality=SimpleNamespace(legal=lambda action: True))
    history = SimpleNamespace(root_tick=110, deployments=())
    with pytest.raises(IncompatibleHistory, match="exhausted"):
        session._opponent_action(info, history)
    assert calls == [1, 1, 1]
    def timeout():
        raise TimeoutError("deadline")
    with pytest.raises(TimeoutError):
        session._opponent_action(info, history, check_deadline=timeout)
    assert len(calls) == 3


def test_incremental_replay_matches_cold_and_rolls_back_on_failure(rollout, monkeypatch):
    monkeypatch.setattr(history_belief, "validate_fair_view", lambda *args: None)
    initial = rollout.freeze_due_information()[0]
    opening = OpeningEvidence(initial.sha256, 0, 100, HOG_DECK)
    opponent = WaitOpponent()
    with rollout.snapshot(None) as start:
        rollout.commit_simultaneous({0: FullAction(), 1: FullAction()})
        for step in range(60):
            rollout.advance()
        middle = rollout.freeze_due_information()[0]
        rollout.commit_simultaneous({0: FullAction(), 1: FullAction()})
        for step in range(60):
            rollout.advance()
        end = rollout.freeze_due_information()[0]
        start.restore()
        session = HistoryReplaySession(rollout, opening=opening, opponent=opponent, seed=17, sidecar_factory=None)
        history = FairHistory(opening, (OwnDecision(100, initial.sha256, FullAction()),), (), 160, middle.sha256)
        assert session.sync(history, middle).sha256 == middle.sha256
        extended = replace(history, decisions=(*history.decisions, OwnDecision(160, middle.sha256, FullAction())),
            root_tick=220, root_fair_sha=end.sha256)
        bad_info = replace(end, frame={"bad": True})
        bad = replace(extended, root_fair_sha=bad_info.sha256)
        with pytest.raises(IncompatibleHistory, match="root differs"):
            session.sync(bad, bad_info)
        assert rollout.runtime.clock.tick == 160 and session.history == history
        assert session.sync(extended, end).sha256 == end.sha256
        assert session.profile["ticks"] == 120 and session.profile["incremental_ticks"] == 60
        session.close()
        start.restore()
        cold = HistoryReplaySession(rollout, opening=opening, opponent=opponent, seed=17, sidecar_factory=None)
        assert cold.sync(extended, end).sha256 == end.sha256
        assert cold.profile["cold_ticks"] == 120
        assert set(opponent.owners) == {1}
        cold.close()


def test_factory_retries_only_incompatible_worlds_and_releases_failed_attempts():
    factory = HistoryBeliefFactory(None, catalog=None, opponent=None, sidecar_factory=None)
    calls, closed, disconnected = [], [], []

    def create(history, *, seed):
        ordinal = len(calls)
        calls.append(seed)

        def sync(*args, **kwargs):
            if ordinal < 2:
                raise IncompatibleHistory("different legal draw")

        return SimpleNamespace(sync=sync, close=lambda: closed.append(ordinal),
            rollout=SimpleNamespace(runtime=SimpleNamespace(environment=SimpleNamespace(
                native=SimpleNamespace(close_transport=lambda: disconnected.append(ordinal))))))

    factory.create = create
    session = factory.build(None, None, seed=55, attempts=3)
    assert len(set(calls)) == 3 and closed == disconnected == [0, 1]
    session.close()
    session.rollout.runtime.environment.native.close_transport()
    assert closed == disconnected == [0, 1, 2]


@pytest.mark.parametrize("error", [TimeoutError("deadline"), RuntimeError("native restore failed")])
def test_factory_does_not_retry_protocol_or_deadline_failures(error):
    factory = HistoryBeliefFactory(None, catalog=None, opponent=None, sidecar_factory=None)
    cleanup = []

    def fail(*args, **kwargs):
        raise error

    factory.create = lambda *args, **kwargs: SimpleNamespace(sync=fail,
        close=lambda: cleanup.append("snapshot"), rollout=SimpleNamespace(runtime=SimpleNamespace(
            environment=SimpleNamespace(native=SimpleNamespace(close_transport=lambda: cleanup.append("transport"))))))
    with pytest.raises(type(error)):
        factory.build(None, None, seed=55, attempts=3)
    assert cleanup == ["snapshot", "transport"]


def test_sync_deadline_restores_previous_root_without_consuming_history(rollout):
    initial = rollout.freeze_due_information()[0]
    opening = OpeningEvidence(initial.sha256, 0, 100, HOG_DECK)
    session = HistoryReplaySession(rollout, opening=opening, opponent=WaitOpponent(), seed=1, sidecar_factory=None)
    target = replace(initial, tick=160)
    history = FairHistory(opening, (OwnDecision(100, initial.sha256, FullAction()),), (), 160, target.sha256)

    def timeout():
        raise TimeoutError("deadline")

    with pytest.raises(TimeoutError):
        session.sync(history, target, check_deadline=timeout)
    assert session.history.root_tick == 100 and session.profile["syncs"] == 0
    assert rollout.freeze_due_information()[0].sha256 == initial.sha256
    session.close()
    session.close()
    with pytest.raises(ValueError, match="identity"):
        session.sync(history, target)
