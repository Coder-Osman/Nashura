from copy import deepcopy
from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_final_history_provider import Session, LeasedWorld, roots
from native_runner.tests.test_fair_final_prior_history import factory
from native_runner.training.fair_final import prior_provider
from native_runner.training.fair_final.history_provider import HistorySearchProvider
from native_runner.training.fair_final.prior_provider import PriorHistorySearchProvider
from native_runner.tests.test_fair_final_rollout import rollout
from native_runner.tests.test_fair_final_search import runtime_world
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.history_belief import HistoryReplaySession
from native_runner.training.fair_final.opening_belief import OpeningEvidence
from native_runner.training.fair_final.policy import FullAction, JointPolicy
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits
from native_runner.training.fair_search.config import HOG_DECK
import torch


def prepared(monkeypatch):
    first, initial, _, _ = roots()
    target = factory()
    sessions, seeds = [], []
    evidence = dict(source="independent-belief", actual_state_copied=False,
        root_fair_sha=initial.sha256, history_sha=first.sha256, owner=initial.owner,
        root_tick=initial.tick, native_draw_verified=True, sampler_sha=target.sampler_sha,
        prior_sha=target.prior.sha256, opponent_controller_sha=target.opponent.controller_sha,
        opponent_checkpoint_sha=target.opponent.checkpoint_sha,
        scope="native-fixed-hog-prior-integration", training_admissible=False)

    def build(history, information, **kwargs):
        kwargs["check_deadline"]()
        seeds.append(kwargs["seed"])
        session = Session(history, information)
        session.conditioning_manifest = deepcopy(evidence)
        sessions.append(session)
        return session

    monkeypatch.setattr(target, "build", build)
    monkeypatch.setattr(prior_provider, "RolloutSearchWorld", LeasedWorld)
    return target, first, initial, evidence, sessions, seeds


def test_prior_provider_binds_prior_and_retains_conditioning(monkeypatch):
    target, history, info, evidence, sessions, seeds = prepared(monkeypatch)
    provider = PriorHistorySearchProvider(target, history=history)
    assert provider.sampler_sha != HistorySearchProvider(target, history=history).sampler_sha
    manifests = []
    for seed in (17, 17, 18):
        world = provider.build(info, seed=seed)
        manifests.append(deepcopy(world.manifest))
        assert world.manifest["conditioning"] == evidence
        sessions[-1].conditioning_manifest["root_tick"] = -1
        assert world.manifest["conditioning"]["root_tick"] == info.tick
        with pytest.raises(ValueError, match="leased"):
            provider.build(info, seed=seed)
        world.close()
        world.close()
        assert provider.active is None
    assert manifests[0] == manifests[1] != manifests[2]
    assert seeds[0] == seeds[1] != seeds[2]
    assert all(session.closed == session.disconnected == 1 for session in sessions)
    provider.close()
    with pytest.raises(ValueError, match="closed"):
        provider.build(info, seed=17)


@pytest.mark.parametrize("field,value", [("prior_sha", "0"*64), ("root_tick", 101),
    ("native_draw_verified", False), ("native_draw_verified", 1),
    ("actual_state_copied", True), ("training_admissible", True),
    ("opponent_controller_sha", "0"*64), ("history_sha", "0"*64)])
def test_prior_conditioning_mismatch_releases_before_snapshot(monkeypatch, field, value):
    target, history, info, evidence, sessions, _ = prepared(monkeypatch)
    evidence[field] = value
    provider = PriorHistorySearchProvider(target, history=history)
    with pytest.raises(ValueError, match="conditioning"):
        provider.build(info, seed=17)
    assert provider.active is None
    assert sessions[0].closed == sessions[0].disconnected == 1


@pytest.mark.parametrize("failure", ["snapshot", "release"])
def test_prior_snapshot_failures_release_transport(monkeypatch, failure):
    target, history, info, _, sessions, _ = prepared(monkeypatch)
    provider = PriorHistorySearchProvider(target, history=history)
    def fail(*args, **kwargs):
        raise RuntimeError("snapshot failure")
    if failure == "snapshot":
        monkeypatch.setattr(prior_provider, "RolloutSearchWorld", fail)
    with pytest.raises(RuntimeError, match="snapshot failure"):
        world = provider.build(info, seed=17)
        world.error = RuntimeError("snapshot failure")
        provider.close()
    assert provider.active is None
    assert sessions[0].closed == sessions[0].disconnected == 1


def test_prior_provider_rejects_unverified_factory():
    with pytest.raises(ValueError, match="verified prior-history"):
        PriorHistorySearchProvider(SimpleNamespace(), history=roots()[0])


@pytest.mark.parametrize("owner", [0, 1])
def test_prior_full128_uses_one_world_and_restores_both_seats(rollout, monkeypatch, owner):
    baseline = runtime_world(rollout, owner=owner)
    baseline.close()
    info = rollout.freeze_due_information()[owner]
    history = FairHistory(OpeningEvidence(info.sha256, owner, info.tick, HOG_DECK),
        (), (), info.tick, info.sha256)
    sessions, disconnected = [], []
    rollout.runtime.environment.native = SimpleNamespace(close_transport=lambda: disconnected.append(True))

    class Inference:
        checkpoint_sha = "c" * 64
        controller_sha = "b" * 64

        def predict(self, information):
            return JointPolicy(lambda prefix: torch.zeros_like(information.legality.mask(prefix),
                dtype=torch.float32), information.legality), torch.zeros(1, 3)

        def sample(self, information, generator):
            return FullAction()

    inference = Inference()
    target = factory()
    target.opponent = inference

    def build(evidence, information, *, seed, attempts, check_deadline):
        session = HistoryReplaySession(rollout, opening=evidence.opening, opponent=inference,
            seed=seed, sidecar_factory=None)
        session.sync(evidence, information, check_deadline=check_deadline)
        session.conditioning_manifest = dict(source="independent-belief", actual_state_copied=False,
            root_fair_sha=information.sha256, history_sha=evidence.sha256, owner=owner,
            root_tick=information.tick, native_draw_verified=True, sampler_sha=target.sampler_sha,
            prior_sha=target.prior.sha256, opponent_controller_sha=inference.controller_sha,
            opponent_checkpoint_sha=inference.checkpoint_sha,
            scope="native-fixed-hog-prior-integration", training_admissible=False)
        sessions.append(session)
        return session

    monkeypatch.setattr(target, "build", build)
    provider = PriorHistorySearchProvider(target, history=history)
    limits = SearchLimits()
    identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
        checkpoint_blue_sha=inference.checkpoint_sha, checkpoint_red_sha=inference.checkpoint_sha,
        controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
    outcome = CompletedBoundarySearch(provider, inference, inference, limits=limits).run(
        info, seed=17, behavior_seed=18, codec=ActionCodec([11, 22, 33, 44, 55, 66, 77, 88]),
        identity=identity)
    assert outcome.result["valid_simulations"] == sum(count for _, count in outcome.policy_target) == 128
    assert outcome.result["value_perspective"] == owner
    assert len(sessions) == 1 and sessions[0].closed and disconnected == [True]
    assert provider.active is None and rollout.freeze_due_information()[owner].sha256 == info.sha256
    assert outcome.audit["world_manifest"]["conditioning"] == sessions[0].conditioning_manifest
    provider.close()
    assert disconnected == [True]
