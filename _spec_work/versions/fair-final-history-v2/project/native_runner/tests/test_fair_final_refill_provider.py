from copy import deepcopy

import pytest

from native_runner.tests.test_fair_final_history_provider import roots
from native_runner.tests.test_fair_final_local_search_provider import Conditioner, LocalFactory, World
from native_runner.training.fair_final import local_reconditioning, refill_local


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(refill_local, "RefillDrawReconditioner", Conditioner)
    monkeypatch.setattr(local_reconditioning, "RolloutSearchWorld", World)
    first, _, _, _ = roots()
    instance = refill_local.RefillLocalSearchProvider(LocalFactory(), history=first, anchor_seed=17)
    yield instance
    instance.close()


def test_refill_provider_keeps_anchor_across_paired_and_successor_roots(provider):
    _, info, following, later = roots()
    original = None
    manifests = []
    for history, information in ((provider.history, info), (provider.history, info), (following, later)):
        provider.history = history
        world = provider.build(information, seed=41)
        session = provider.cursor.session
        original = session if original is None else original
        assert session is original and provider.active is world
        manifests.append(deepcopy(world.manifest))
        world.close()
        assert provider.active is None and session.state == "anchor"
    assert manifests[0] == manifests[1]
    assert manifests[2]["anchor_history_sha"] == following.sha256
    assert len(provider.factory.calls) == 1
    assert [event["mode"] for event in provider.cursor.events] == ["cold", "incremental", "incremental"]
    provider.close()
    assert original.closed == original.disconnected == 1


@pytest.mark.parametrize("failure", ["conditioning", "construction", "restore", "deadline"])
def test_refill_provider_seals_failed_lifecycle_and_disconnects(provider, monkeypatch, failure):
    _, information, _, _ = roots()
    world = provider.build(information, seed=41)
    session = provider.cursor.session
    if failure == "restore":
        session.restore_error = RuntimeError("injected refill failure")
        operation = world.close
    else:
        world.close()
        if failure == "conditioning":
            provider.conditioner.error = RuntimeError("injected refill failure")
        elif failure == "construction":
            def fail(*args, **kwargs):
                raise RuntimeError("injected refill failure")
            monkeypatch.setattr(local_reconditioning, "RolloutSearchWorld", fail)
        else:
            def fail():
                raise RuntimeError("injected refill failure")
            provider.cursor.check_deadline = fail
        operation = lambda: provider.build(information, seed=42)
    with pytest.raises(RuntimeError, match="injected refill failure"):
        operation()
    assert provider.closed and provider.cursor.closed and provider.active is None
    assert session.closed == session.disconnected == 1
    with pytest.raises(ValueError, match="closed"):
        provider.build(information, seed=43)


def test_refill_old_lease_cannot_clear_new_lease(provider):
    _, information, _, _ = roots()
    old = provider.build(information, seed=41)
    old.close()
    current = provider.build(information, seed=42)
    old.close()
    assert provider.active is current and not current.closed
    provider.close()
    current.close()
    assert provider.active is None and current.closes == 1
