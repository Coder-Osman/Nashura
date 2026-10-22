from copy import deepcopy
from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_final_history_provider import Factory, Session, roots
from native_runner.training.fair_final import local_reconditioning as local
from native_runner.training.fair_final.history_belief import IncompatibleHistory


class LocalSession(Session):
    def __init__(self, history, info):
        super().__init__(history, info)
        self.state = "anchor"
        self.restores = 0
        self.restore_error = None
        self.conditioning_manifest = dict(source="independent-belief", actual_state_copied=False)
        self.root = SimpleNamespace(restore=self.restore)
        self.rollout.session = self

    def restore(self):
        self.restores += 1
        if self.restore_error is not None:
            raise self.restore_error
        self.state = "anchor"


class LocalFactory(Factory):
    def build(self, history, info, **kwargs):
        kwargs["check_deadline"]()
        self.calls.append(kwargs["seed"])
        if self.error is not None:
            raise self.error
        session = LocalSession(history, info)
        self.sessions.append(session)
        return session


class Conditioner:
    sampler_sha = "d" * 64

    def __init__(self, factory):
        self.calls = []
        self.error = None

    def apply(self, session, history, info, *, seed, check_deadline):
        check_deadline()
        self.calls.append((session, history, seed))
        if self.error is not None:
            raise self.error
        assert session.state == "anchor"
        session.state = ("conditioned", seed)
        return dict(manifest=dict(source="independent-belief", actual_state_copied=False,
            root_fair_sha=info.sha256, seed=seed, training_admissible=False),
            seconds=0.25, cycle_delta=dict(ticks=0))


class World:
    def __init__(self, rollout, **kwargs):
        self.rollout = rollout
        self.manifest = deepcopy(kwargs["manifest"])
        self.closed = False
        self.closes = 0
        self.error = None

    def close(self):
        self.closes += 1
        if self.error is not None:
            raise self.error
        self.closed = True


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(local, "SettledDrawReconditioner", Conditioner)
    monkeypatch.setattr(local, "RolloutSearchWorld", World)
    first, _, _, _ = roots()
    instance = local.SettledLocalSearchProvider(LocalFactory(), history=first, anchor_seed=17)
    yield instance
    instance.close()


def test_repeated_roots_reuse_anchor_but_draw_each_search_seed(provider):
    _, info, _, _ = roots()
    worlds = []
    for seed in (11, 12, 11):
        world = provider.build(info, seed=seed)
        worlds.append(world)
        assert provider.active is world
        with pytest.raises(ValueError, match="leased"):
            provider.build(info, seed=seed)
        world.close()
        world.close()
        assert provider.active is None and not provider.closed
        assert provider.cursor.session.state == "anchor"
        assert world.closes == 1
    assert len(provider.factory.sessions) == 1
    assert len(provider.conditioner.calls) == 3
    assert worlds[0].manifest == worlds[2].manifest != worlds[1].manifest
    assert worlds[0].manifest["root_sampling"] == "fresh-draw-on-independent-incremental-anchor"
    assert worlds[0].manifest["training_admissible"] is False
    assert [world.local_profile["anchor_event"]["mode"] for world in worlds] == ["cold", "incremental", "incremental"]
    assert provider.factory.sessions[0].restores == 3
    provider.close()
    provider.close()
    assert provider.factory.sessions[0].closed == provider.factory.sessions[0].disconnected == 1


def test_successor_root_keeps_session_and_updates_verified_history(provider):
    _, info, following, later = roots()
    provider.build(info, seed=11).close()
    session = provider.cursor.session
    provider.history = following
    world = provider.build(later, seed=12)
    assert provider.cursor.session is session
    assert world.manifest["anchor_history_sha"] == following.sha256
    assert world.local_profile["anchor_event"]["mode"] == "incremental"
    assert world.local_profile["anchor_event"]["from_tick"] == info.tick
    assert world.local_profile["anchor_event"]["to_tick"] == later.tick
    world.close()


def test_successor_incompatibility_rebuilds_explicitly(provider):
    _, info, following, later = roots()
    provider.build(info, seed=11).close()
    previous = provider.cursor.session
    previous.sync_error = IncompatibleHistory("new public card")
    provider.history = following
    world = provider.build(later, seed=12)
    assert provider.cursor.session is not previous
    assert previous.closed == previous.disconnected == 1
    assert world.local_profile["anchor_event"]["mode"] == "rebuild"
    world.close()


@pytest.mark.parametrize("failure", ["conditioning", "construction", "sync", "deadline"])
def test_build_failures_close_anchor_transport(provider, monkeypatch, failure):
    _, info, _, _ = roots()
    provider.build(info, seed=11).close()
    session = provider.cursor.session
    def fail(*args, **kwargs):
        raise RuntimeError("injected failure")
    if failure == "conditioning":
        provider.conditioner.error = RuntimeError("injected failure")
    elif failure == "construction":
        monkeypatch.setattr(local, "RolloutSearchWorld", fail)
    elif failure == "sync":
        session.sync_error = RuntimeError("injected failure")
    else:
        provider.cursor.check_deadline = fail
    with pytest.raises(RuntimeError, match="injected failure"):
        provider.build(info, seed=12)
    assert provider.closed and provider.cursor.closed and provider.active is None
    assert session.closed == session.disconnected == 1
    with pytest.raises(ValueError, match="closed"):
        provider.build(info, seed=12)


@pytest.mark.parametrize("failure", ["world-close", "anchor-restore"])
@pytest.mark.parametrize("through_provider", [False, True])
def test_release_failure_seals_provider_and_releases_transport(provider, failure, through_provider):
    _, info, _, _ = roots()
    world = provider.build(info, seed=11)
    session = provider.cursor.session
    if failure == "world-close":
        world.error = RuntimeError("injected release failure")
    else:
        session.restore_error = RuntimeError("injected release failure")
    with pytest.raises(RuntimeError, match="injected release failure"):
        (provider.close if through_provider else world.close)()
    assert provider.closed and provider.cursor.closed and provider.active is None
    assert session.closed == session.disconnected == 1
    assert session.restores == 1 and world.closes == 1
    world.close()
    provider.close()
    with pytest.raises(ValueError, match="closed"):
        provider.build(info, seed=12)


def test_provider_close_with_active_lease_releases_both_once(provider):
    _, info, _, _ = roots()
    world = provider.build(info, seed=11)
    session = provider.cursor.session
    provider.close()
    world.close()
    provider.close()
    assert world.closed and world.closes == session.restores == 1
    assert session.closed == session.disconnected == 1
    assert provider.active is None


def test_old_lease_close_does_not_clear_new_lease(provider):
    _, info, _, _ = roots()
    old = provider.build(info, seed=11)
    old.close()
    current = provider.build(info, seed=12)
    old.close()
    assert provider.active is current and not current.closed
    current.close()


def test_bad_information_closes_existing_anchor_without_reconditioning(provider):
    _, info, _, later = roots()
    provider.build(info, seed=11).close()
    session = provider.cursor.session
    with pytest.raises(ValueError, match="verified FAIR prefix"):
        provider.build(later, seed=12)
    assert len(provider.conditioner.calls) == 1
    assert provider.closed and session.closed == session.disconnected == 1
