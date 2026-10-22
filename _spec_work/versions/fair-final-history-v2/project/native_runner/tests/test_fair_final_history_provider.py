from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_final_history import history_fixture, opening_fixture
from native_runner.tests.test_fair_final_rollout import rollout
from native_runner.tests.test_fair_final_search import runtime_world
from native_runner.training.fair_final import history_provider
from native_runner.training.fair_final.history import FairHistory, OwnDecision
from native_runner.training.fair_final.history_belief import HistoryReplaySession, IncompatibleHistory
from native_runner.training.fair_final.history_provider import HistorySearchProvider, RecoveringHistoryCursor
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.opening_belief import OpeningEvidence
from native_runner.training.fair_final.policy import FullAction, JointPolicy
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_final.timing import Reservation


class Session:
    def __init__(self, history, info):
        self.history, self.info = history, info
        self.sync_error = None
        self.closed = self.disconnected = 0
        self.rollout = SimpleNamespace(runtime=SimpleNamespace(environment=SimpleNamespace(
            native=SimpleNamespace(close_transport=self.disconnect))))

    def sync(self, history, info, **kwargs):
        kwargs["check_deadline"]()
        if self.sync_error is not None:
            raise self.sync_error
        self.history, self.info = history, info

    def close(self):
        self.closed += 1

    def disconnect(self):
        self.disconnected += 1


class Factory:
    sampler_sha = "a" * 64
    sidecar_factory = None
    opponent = SimpleNamespace(controller_sha="b" * 64, checkpoint_sha="c" * 64)

    def __init__(self):
        self.calls, self.sessions = [], []
        self.error = None

    def build(self, history, info, **kwargs):
        kwargs["check_deadline"]()
        self.calls.append(kwargs["seed"])
        if self.error is not None:
            raise self.error
        session = Session(history, info)
        self.sessions.append(session)
        return session


def roots():
    first = history_fixture()
    _, initial = opening_fixture()
    later = replace(initial, tick=160)
    following = replace(first, decisions=(OwnDecision(100, initial.sha256, FullAction()),),
        root_tick=160, root_fair_sha=later.sha256)
    return first, initial, following, later


def test_cursor_uses_delta_then_bounded_rebuild_without_consuming_failed_prefix():
    first, initial, following, later = roots()
    factory = Factory()
    cursor = RecoveringHistoryCursor(factory, seed=17, attempts=2)
    old = cursor.sync(first, initial)
    assert cursor.sync(first, initial) is old and len(factory.calls) == 1
    old.sync_error = IncompatibleHistory("new public card invalidates sampled hidden world")
    factory.error = IncompatibleHistory("bounded attempts exhausted")
    with pytest.raises(IncompatibleHistory, match="exhausted"):
        cursor.sync(following, later)
    assert old.closed == old.disconnected == 1
    assert cursor.session is None and cursor.history == first
    assert cursor.events[-1]["status"] == "failed"
    assert cursor.events[-1]["mode"] == "rebuild"
    factory.error = None
    replacement = cursor.sync(following, later)
    assert cursor.history == following and replacement is not old
    assert factory.calls[-2] == factory.calls[-1] != factory.calls[0]
    cursor.close()
    cursor.close()
    assert replacement.closed == replacement.disconnected == 1


def test_cursor_rebuild_success_is_explicit_not_an_incremental_speed_claim():
    first, initial, following, later = roots()
    cursor = RecoveringHistoryCursor(Factory(), seed=17)
    old = cursor.sync(first, initial)
    old.sync_error = IncompatibleHistory("mismatch")
    assert cursor.sync(following, later) is not old
    assert cursor.events[-1]["mode"] == "rebuild"
    assert cursor.events[-1]["reason"] == "mismatch"
    cursor.close()


@pytest.mark.parametrize("error", [RuntimeError("restore failed"), TimeoutError("deadline")])
def test_cursor_does_not_rebuild_on_protocol_or_deadline_error(error):
    first, initial, following, later = roots()
    factory = Factory()
    cursor = RecoveringHistoryCursor(factory, seed=17)
    old = cursor.sync(first, initial)
    old.sync_error = error
    with pytest.raises(type(error)):
        cursor.sync(following, later)
    assert len(factory.calls) == 1 and cursor.history == first
    cursor.close()
    assert old.closed == old.disconnected == 1


def test_cursor_rejects_foreign_frame_or_history_before_engine_access():
    first, initial, following, later = roots()
    factory = Factory()
    cursor = RecoveringHistoryCursor(factory, seed=17)
    for history, info in ((first.to_payload(), initial), (first, later),
            (first, replace(initial, owner=1)), (first, replace(initial, frame={"tampered": True}))):
        with pytest.raises(ValueError, match="verified FAIR prefix"):
            cursor.sync(history, info)
    assert not factory.calls
    cursor.sync(following, later)
    with pytest.raises(ValueError):
        cursor.sync(first, initial)
    assert len(factory.calls) == 1
    cursor.close()
    with pytest.raises(ValueError):
        cursor.sync(following, later)


class LeasedWorld:
    def __init__(self, rollout, **kwargs):
        self.manifest = kwargs["manifest"]
        self.closed = False
        self.error = None

    def close(self):
        self.closed = True
        if self.error is not None:
            raise self.error


def test_provider_draws_fresh_per_root_and_releases_each_lease(monkeypatch):
    monkeypatch.setattr(history_provider, "RolloutSearchWorld", LeasedWorld)
    first, initial, _, _ = roots()
    factory = Factory()
    provider = HistorySearchProvider(factory, history=first)
    worlds = []
    for seed in (17, 17, 18):
        world = provider.build(initial, seed=seed)
        worlds.append(world)
        with pytest.raises(ValueError, match="leased"):
            provider.build(initial, seed=seed)
        world.close()
        world.close()
        assert provider.active is None
    assert len(factory.sessions) == 3
    assert factory.calls[0] == factory.calls[1] != factory.calls[2]
    assert worlds[0].manifest == worlds[1].manifest != worlds[2].manifest
    assert worlds[0].manifest["training_admissible"] is False
    assert all(session.closed == session.disconnected == 1 for session in factory.sessions)
    provider.close()
    with pytest.raises(ValueError, match="closed"):
        provider.build(initial, seed=17)


@pytest.mark.parametrize("failure_at", ["construction", "release"])
def test_provider_failure_releases_session_transport_and_lease(monkeypatch, failure_at):
    first, initial, _, _ = roots()
    factory = Factory()
    provider = HistorySearchProvider(factory, history=first)
    def fail(*args, **kwargs):
        raise RuntimeError("snapshot failed")
    monkeypatch.setattr(history_provider, "RolloutSearchWorld",
        fail if failure_at == "construction" else LeasedWorld)
    with pytest.raises(RuntimeError, match="snapshot failed"):
        world = provider.build(initial, seed=17)
        world.error = RuntimeError("snapshot failed")
        provider.close()
    assert provider.active is None
    assert factory.sessions[0].closed == factory.sessions[0].disconnected == 1


@pytest.mark.parametrize("attempts", [0, 33, True, 1.5])
def test_retry_bounds_are_validated_before_access(attempts):
    with pytest.raises(ValueError):
        RecoveringHistoryCursor(Factory(), seed=1, attempts=attempts)
    with pytest.raises(ValueError):
        HistorySearchProvider(Factory(), history=history_fixture(), attempts=attempts)


@pytest.mark.parametrize("owner", [0, 1])
def test_search_world_accepts_only_root_ready_preserves_other_pending_and_restores(rollout, owner):
    opening = runtime_world(rollout, owner=owner)
    opening.close()
    enemy = 1-owner
    clock = rollout.runtime.clock
    clock.pending[enemy] = Reservation(0, enemy, FullAction(), 100, 160)
    clock.next_identity = 1
    clock.ready = {owner}
    rollout.runtime.frozen = {owner: rollout.runtime.frozen[owner]}
    rollout.information = {}
    info = rollout.freeze_due_information()[owner]
    world = history_provider.RolloutSearchWorld(rollout, owner=owner, sidecar_factory=None,
        manifest=dict(source="independent-belief", actual_state_copied=False, root_fair_sha=info.sha256))
    def unexpected_sample(*args):
        raise AssertionError("pending opponent must not choose again before its wake")
    pending = clock.pending[enemy]
    for attempt in range(2):
        world.restore_root()
        result = world.transition(FullAction(11, 0, 1),
            opponent=SimpleNamespace(sample=unexpected_sample), generator=torch.Generator(),
            hard_tick=200, maximum_decisions=2, check_deadline=lambda: None)
        assert result.completed and result.reason == "executed" and result.ticks == 2
        assert result.information.owner == owner
    world.close()
    assert rollout.runtime.clock.ready == {owner}
    assert rollout.runtime.clock.pending[enemy] == pending
    assert rollout.freeze_due_information()[owner].sha256 == info.sha256


def test_search_provider_full128_integration_builds_once_and_cleans_runtime(rollout):
    baseline = runtime_world(rollout)
    baseline.close()
    info = rollout.freeze_due_information()[0]
    opening = OpeningEvidence(info.sha256, 0, info.tick, HOG_DECK)
    history = FairHistory(opening, (), (), info.tick, info.sha256)
    disconnected, sessions = [], []
    rollout.runtime.environment.native = SimpleNamespace(close_transport=lambda: disconnected.append(True))
    class Inference:
        checkpoint_sha = "c" * 64
        controller_sha = "b" * 64

        def predict(self, information):
            policy = JointPolicy(lambda prefix: torch.zeros_like(information.legality.mask(prefix),
                dtype=torch.float32), information.legality)
            return policy, torch.zeros(1, 3)

        def sample(self, information, generator):
            return FullAction()

    inference = Inference()
    factory = Factory()
    factory.opponent = inference
    def build(evidence, information, *, seed, attempts, check_deadline):
        check_deadline()
        session = HistoryReplaySession(rollout, opening=evidence.opening, opponent=inference,
            seed=seed, sidecar_factory=None)
        session.sync(evidence, information, check_deadline=check_deadline)
        sessions.append(session)
        return session
    factory.build = build
    provider = HistorySearchProvider(factory, history=history)
    limits = SearchLimits(soft_decisions=1, soft_ticks=60)
    identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
        checkpoint_blue_sha=inference.checkpoint_sha, checkpoint_red_sha=inference.checkpoint_sha,
        controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
    search = CompletedBoundarySearch(provider, inference, inference, limits=limits)
    result = search.run(info, seed=17, behavior_seed=18, identity=identity,
        codec=ActionCodec([11, 22, 33, 44, 55, 66, 77, 88]))
    assert result.result["valid_simulations"] == 128
    assert sum(count for action, count in result.policy_target) == 128
    assert len(sessions) == 1 and sessions[0].closed and disconnected == [True]
    assert provider.active is None
    assert rollout.freeze_due_information()[0].sha256 == info.sha256
    provider.close()
    assert disconnected == [True]
