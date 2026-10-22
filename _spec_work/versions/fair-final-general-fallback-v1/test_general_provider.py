from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_final_history_provider import roots
from native_runner.tests.test_fair_final_local_search_provider import Conditioner, LocalFactory, World
from native_runner.tests.test_fair_final_rollout import rollout
from native_runner.tests.test_fair_final_search import runtime_world
from native_runner.training.fair_final import history_provider, local_reconditioning, refill_local
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.conditioned_plan import HistoryConditionedOpponent
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.history_belief import HistoryReplaySession, IncompatibleHistory
from native_runner.training.fair_final.opening_belief import OpeningEvidence
from native_runner.training.fair_final.policy import FullAction, JointPolicy
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits, split_seed
from native_runner.training.fair_final.timing import Reservation
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest

from general_provider import GeneralHistorySearchProvider


class Factory(LocalFactory, refill_local.IncrementalRefillBeliefFactory):
    closed = False


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(refill_local, "RefillDrawReconditioner", Conditioner)
    monkeypatch.setattr(local_reconditioning, "RolloutSearchWorld", World)
    monkeypatch.setattr(history_provider, "RolloutSearchWorld", World)
    first, _, _, _ = roots()
    instance = GeneralHistorySearchProvider(Factory(), history=first, anchor_seed=17)
    yield instance
    instance.close()


def unsupported(provider):
    provider._local_provider().conditioner.conditioner.error = (
        local_reconditioning.UnsupportedLocalBoundary("opponent pending"))


def test_local_reuse_and_manifest_commitment_do_not_mutate_inner_identity(provider):
    _, information, following, later = roots()
    previous = None
    for history, info in ((provider.history, information), (provider.history, information), (following, later)):
        provider.history = history
        world = provider.build(info, seed=41)
        assert world.manifest["mode"] == "local"
        assert world.commitment == semantic_digest(world.manifest)
        assert world.world.manifest["sampler_sha"] == provider.local.sampler_sha
        assert world.manifest["sampler_sha"] == provider.sampler_sha != provider.local.sampler_sha
        assert world.manifest["training_admissible"] is False
        if previous is not None:
            previous.close()
            assert provider.active is world
        with pytest.raises(ValueError, match="leased"):
            provider.build(info, seed=41)
        previous = world
        world.close()
        world.close()
        with pytest.raises(ValueError, match="released"):
            world.restore_root()
    assert len(provider.factory.calls) == 1
    provider.close()
    assert provider.factory.sessions[0].closed == provider.factory.sessions[0].disconnected == 1


def test_unsupported_boundary_releases_anchor_before_independent_rebuild(provider):
    _, info, following, later = roots()
    unsupported(provider)
    original_build = provider.factory.build
    def build(history, information, **kwargs):
        if provider.factory.sessions:
            assert all(session.closed == session.disconnected == 1 for session in provider.factory.sessions)
        return original_build(history, information, **kwargs)
    provider.factory.build = build
    world = provider.build(info, seed=41)
    assert world.manifest["mode"] == "independent-history-rebuild"
    assert world.manifest["fallback_reason"] == "opponent pending"
    assert world.manifest["construction"]["history_sha"] == provider.history.sha256
    assert len(provider.factory.calls) == 2
    assert provider.local.closed and provider.local.cursor.closed
    world.close()
    world.close()
    assert all(session.closed == session.disconnected == 1 for session in provider.factory.sessions)
    provider.history = following
    next_world = provider.build(later, seed=42)
    assert next_world.manifest["mode"] == "local"
    assert provider.verified_history == following
    next_world.close()


@pytest.mark.parametrize("error", [RuntimeError("protocol"), TimeoutError("deadline"),
    IncompatibleHistory("no compatible world")])
def test_conditioning_errors_never_use_fallback(provider, error):
    _, info, _, _ = roots()
    provider._local_provider().conditioner.conditioner.error = error
    with pytest.raises(type(error), match=str(error)):
        provider.build(info, seed=41)
    assert len(provider.factory.calls) == 1 and provider.closed
    assert provider.factory.sessions[0].closed == provider.factory.sessions[0].disconnected == 1


def test_unsupported_error_outside_conditioner_never_activates_fallback(provider):
    _, info, _, _ = roots()
    provider.factory.error = local_reconditioning.UnsupportedLocalBoundary("factory protocol")
    with pytest.raises(local_reconditioning.UnsupportedLocalBoundary, match="factory protocol"):
        provider.build(info, seed=41)
    assert provider.closed and len(provider.factory.calls) == 1


@pytest.mark.parametrize("error", [RuntimeError("transport"), TimeoutError("deadline"),
    IncompatibleHistory("bounded exhaustion")])
def test_fallback_failure_is_terminal_and_does_not_retry_again(provider, error):
    _, info, _, _ = roots()
    unsupported(provider)
    original_build = provider.factory.build
    def build(history, information, **kwargs):
        if provider.factory.calls:
            provider.factory.error = error
        return original_build(history, information, **kwargs)
    provider.factory.build = build
    with pytest.raises(type(error), match=str(error)):
        provider.build(info, seed=41)
    assert provider.closed and len(provider.factory.calls) == 2
    assert provider.factory.sessions[0].closed == provider.factory.sessions[0].disconnected == 1


@pytest.mark.parametrize("field", ["owner", "tick", "frame", "history"])
def test_invalid_root_rejected_before_factory_access(provider, field):
    _, info, _, later = roots()
    invalid = {"owner": replace(info, owner=1), "tick": later,
        "frame": replace(info, frame={"tampered": True}), "history": info}[field]
    if field == "history":
        provider.history = provider.history.to_payload()
    with pytest.raises(ValueError, match="verified FAIR prefix"):
        provider.build(invalid, seed=41)
    assert not provider.factory.calls and provider.closed


def test_fallback_cannot_rewind_verified_prefix(provider):
    first, info, following, later = roots()
    unsupported(provider)
    provider.history = following
    provider.build(later, seed=41).close()
    provider.history = first
    with pytest.raises(ValueError, match="verified FAIR prefix"):
        provider.build(info, seed=42)
    assert len(provider.factory.calls) == 2 and provider.closed


@pytest.mark.parametrize("mode", ["local", "fallback"])
@pytest.mark.parametrize("failure", ["world-close", "restore"])
@pytest.mark.parametrize("through_provider", [False, True])
def test_release_failure_closes_all_handles(provider, mode, failure, through_provider):
    _, info, _, _ = roots()
    if mode == "fallback":
        unsupported(provider)
    world = provider.build(info, seed=41)
    session = provider.factory.sessions[-1]
    if failure == "world-close" or mode == "fallback":
        world.world.error = RuntimeError("release failure")
    else:
        session.restore_error = RuntimeError("release failure")
    with pytest.raises(RuntimeError, match="release failure"):
        (provider.close if through_provider else world.close)()
    assert provider.closed and provider.active is None
    assert all(session.closed == session.disconnected == 1 for session in provider.factory.sessions)
    provider.close()
    world.close()


@pytest.mark.parametrize("mode", ["local", "fallback"])
def test_provider_close_releases_active_lease_once(provider, mode):
    _, info, _, _ = roots()
    if mode == "fallback":
        unsupported(provider)
    world = provider.build(info, seed=41)
    inner = world.world
    provider.close()
    provider.close()
    world.close()
    assert world.closed and inner.closes == 1 and provider.active is None
    assert all(session.closed == session.disconnected == 1 for session in provider.factory.sessions)
    with pytest.raises(ValueError, match="closed"):
        provider.build(info, seed=42)


def test_fallback_preserves_attempt_bound_deadline_and_separates_anchor_rng(provider):
    _, info, _, _ = roots()
    calls = []
    original_build = provider.factory.build
    def build(history, information, **kwargs):
        calls.append(kwargs.copy())
        return original_build(history, information, **kwargs)
    provider.factory.build = build
    unsupported(provider)
    provider.build(info, seed=41).close()
    assert len(calls) == 2
    assert [call["attempts"] for call in calls] == [4, 4]
    assert [call["check_deadline"] for call in calls] == [provider.check_deadline]*2
    assert [call["seed"] for call in calls] == [
        split_seed(seed, "conditional-history", provider.history.sha256) for seed in (17, 41)]


@pytest.mark.parametrize("attempts", [0, 33, True, 1.5])
def test_attempt_bounds_and_factory_type(attempts):
    first, _, _, _ = roots()
    with pytest.raises(ValueError, match="bounded"):
        GeneralHistorySearchProvider(Factory(), history=first, anchor_seed=17, attempts=attempts)
    with pytest.raises(ValueError, match="independent incremental"):
        GeneralHistorySearchProvider(LocalFactory(), history=first, anchor_seed=17)


def test_cleanup_error_after_unsupported_boundary_does_not_rebuild(provider):
    _, info, _, _ = roots()
    provider.build(info, seed=40).close()
    session = provider.factory.sessions[0]
    unsupported(provider)
    def fail():
        session.closed += 1
        raise local_reconditioning.UnsupportedLocalBoundary("cleanup protocol")
    session.close = fail
    with pytest.raises(local_reconditioning.UnsupportedLocalBoundary, match="cleanup protocol"):
        provider.build(info, seed=41)
    assert provider.closed and len(provider.factory.calls) == 1
    assert session.closed == session.disconnected == 1


def test_deadline_between_local_cleanup_and_fallback_prevents_construction(provider):
    _, info, _, _ = roots()
    unsupported(provider)
    def check():
        if provider.local is not None and provider.local.closed:
            raise TimeoutError("deadline after cleanup")
    provider.check_deadline = check
    with pytest.raises(TimeoutError, match="deadline after cleanup"):
        provider.build(info, seed=41)
    assert provider.closed and len(provider.factory.calls) == 1


@pytest.mark.parametrize("field,value", [("source", "actual"), ("actual_state_copied", True),
    ("root_fair_sha", "0"*64), ("sampler_sha", "0"*64), ("training_admissible", True)])
def test_construction_identity_tampering_releases_world(provider, monkeypatch, field, value):
    _, info, _, _ = roots()
    worlds = []
    class TamperedWorld(World):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.manifest[field] = value
            worlds.append(self)
    monkeypatch.setattr(local_reconditioning, "RolloutSearchWorld", TamperedWorld)
    with pytest.raises(ValueError, match="construction identity"):
        provider.build(info, seed=41)
    assert provider.closed and worlds[0].closes == 1
    assert provider.factory.sessions[0].closed == provider.factory.sessions[0].disconnected == 1


@pytest.mark.parametrize("owner", [0, 1])
def test_fixture_pending_full128_real_boundary_selection_and_repeat_restore(rollout, owner):
    baseline = runtime_world(rollout, owner=owner)
    baseline.close()
    enemy = 1-owner
    runtime = rollout.runtime
    runtime.clock.pending[enemy] = Reservation(0, enemy, FullAction(), 100, 160)
    runtime.clock.next_identity = 1
    runtime.clock.ready = {owner}
    runtime.frozen = {owner: runtime.frozen[owner]}
    rollout.information = {}
    info = rollout.freeze_due_information()[owner]
    opening = OpeningEvidence(info.sha256, owner, info.tick, HOG_DECK)
    history = FairHistory(opening, (), (), info.tick, info.sha256)
    disconnected, sessions = [], []
    runtime.environment.native = SimpleNamespace(close_transport=lambda: disconnected.append(True))
    pending = runtime.clock.pending[enemy]
    class Inference:
        checkpoint_sha = "c"*64
        controller_sha = "b"*64

        def predict(self, information):
            policy = JointPolicy(lambda prefix: torch.zeros_like(information.legality.mask(prefix),
                dtype=torch.float32), information.legality)
            return policy, torch.zeros(1, 3)

        def sample(self, information, generator):
            return FullAction()

    inference = Inference()
    factory = Factory()
    factory.prior = SimpleNamespace(sha256="e"*64)
    factory.opponent = inference
    def build(evidence, information, *, seed, attempts, check_deadline):
        check_deadline()
        if sessions:
            assert sessions[-1].closed
        session = HistoryReplaySession(rollout, opening=evidence.opening, opponent=inference,
            seed=seed, sidecar_factory=None)
        session.sync(evidence, information, check_deadline=check_deadline)
        session.opponent = HistoryConditionedOpponent(inference, evidence)
        session.conditioning_manifest = dict(source="independent-belief", actual_state_copied=False,
            sampler_sha=factory.sampler_sha, prior_sha=factory.prior.sha256,
            root_fair_sha=evidence.root_fair_sha, history_sha=evidence.sha256, native_draw_verified=True)
        sessions.append(session)
        return refill_local.IncrementalRefillSession(session, evidence, inference)
    factory.build = build
    provider = GeneralHistorySearchProvider(factory, history=history, anchor_seed=31)
    limits = SearchLimits(soft_decisions=1, soft_ticks=60)
    identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
        checkpoint_blue_sha=inference.checkpoint_sha, checkpoint_red_sha=inference.checkpoint_sha,
        controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
    search = CompletedBoundarySearch(provider, inference, inference, limits=limits)
    results = []
    try:
        for attempt in range(2):
            result = search.run(info, seed=17, behavior_seed=18, identity=identity,
                codec=ActionCodec([11, 22, 33, 44, 55, 66, 77, 88]))
            results.append(result)
            assert result.result["valid_simulations"] == 128
            assert result.result["unfinished_edge_backups"] == 0
            assert sum(count for action, count in result.policy_target) == 128
            assert result.audit["world_manifest"]["mode"] == "independent-history-rebuild"
            assert all(session.closed for session in sessions)
            assert len(disconnected) == len(sessions) == 2*(attempt+1)
            assert provider.active is None and not provider.closed
            assert runtime.clock.pending[enemy] == pending and runtime.clock.ready == {owner}
            assert rollout.freeze_due_information()[owner].sha256 == info.sha256
        assert results[0].policy_target == results[1].policy_target
        assert results[0].selected == results[1].selected
        assert results[0].audit["world_manifest"] == results[1].audit["world_manifest"]
    finally:
        provider.close()
    assert len(disconnected) == 4
