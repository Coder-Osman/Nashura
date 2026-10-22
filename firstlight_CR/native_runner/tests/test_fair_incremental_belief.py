from dataclasses import replace
from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_search import information, frame, ToyInference, ToyWorld
from native_runner.training.fair_search.belief import IncrementalBeliefProvider, IncompatibleBelief
from native_runner.training.fair_search.config import SearchConfig
from native_runner.training.fair_search.native import NativeReplayFactory
from native_runner.training.fair_search.search import InformationSetSearch


class UpdatingFactory:
    def __init__(self):
        self.created = []
        self.updated = []
        self.failure = None
        self.failures = {}

    def __call__(self, information, *, seed, ordinal):
        world = ToyWorld(information)
        self.created.append(world)
        return world

    def update(self, world, information):
        self.updated.append((world, information))
        world.restore_root()
        failure = self.failures.get(id(world), self.failure)
        if failure is not None:
            raise failure
        world.root = information


def test_search_retains_only_clean_roots_and_releases_at_episode_end(information):
    factory = UpdatingFactory()
    provider = IncrementalBeliefProvider(factory)
    config = SearchConfig(particles=1)
    search = InformationSetSearch(provider, ToyInference(), config=config)
    for tick in (100, 200, 300):
        root = replace(information, tick=tick)
        result = search.run(root, seed=7, training=False)
        assert sum(result.visits.values()) == 32
        assert all(value >= 0 for value in result.timings.values())
        assert provider.worlds[0].current.sha256 == root.sha256
        assert provider.worlds[0].action is None
        assert not provider.leased
    assert len(factory.created) == 1
    assert provider.profile["reused"] == 2
    assert provider.profile["incremental_ticks"] == 200
    provider.close()
    provider.close()
    assert factory.created[0].closed
    assert provider.worlds == []


def test_incompatible_particle_is_released_and_rebuilt(information):
    factory = UpdatingFactory()
    provider = IncrementalBeliefProvider(factory)
    provider.build(information, seed=1, limit=1)[0].close()
    factory.failure = IncompatibleBelief("new own draw contradicts sampled world")
    lease = provider.build(replace(information, tick=101), seed=2, limit=1)[0]
    assert factory.created[0].closed
    assert len(factory.created) == 2
    assert provider.profile["invalidated"] == 1
    assert lease.root_information().tick == 101
    lease.close()
    provider.close()


def test_reserve_world_replaces_an_invalidated_active_world_without_rebuild(information):
    factory = UpdatingFactory()
    provider = IncrementalBeliefProvider(factory, reserve=1)
    provider.build(information, seed=1, limit=1)[0].close()
    active, reserve = provider.active_worlds[0], provider.worlds[1]
    factory.failures[id(active)] = IncompatibleBelief("new own draw contradicts sampled world")

    lease = provider.build(replace(information, tick=101), seed=2, limit=1)[0]
    assert lease.world is reserve
    assert active.closed
    assert len(factory.created) == 2
    assert provider.profile["reserve_reused"] == 1
    assert provider.profile["rebuild_avoided"] == 1
    lease.close()
    provider.close()


def test_invalidated_reserve_does_not_rebuild_while_active_world_survives(information):
    factory = UpdatingFactory()
    provider = IncrementalBeliefProvider(factory, reserve=1)
    provider.build(information, seed=1, limit=1)[0].close()
    active, reserve = provider.active_worlds[0], provider.worlds[1]
    factory.failures[id(reserve)] = IncompatibleBelief("public play contradicts sampled world")

    lease = provider.build(replace(information, tick=101), seed=2, limit=1)[0]
    assert lease.world is active
    assert reserve.closed
    assert len(factory.created) == 2
    assert provider.profile["reserve_invalidated"] == 1
    lease.close()
    provider.close()


def test_restore_failure_aborts_instead_of_rebuilding(information):
    factory = UpdatingFactory()
    provider = IncrementalBeliefProvider(factory)
    provider.build(information, seed=1, limit=1)[0].close()
    factory.failure = ValueError("world-local root restore checksum differs")
    with pytest.raises(ValueError, match="checksum"):
        provider.build(replace(information, tick=101), seed=2, limit=1)
    assert len(factory.created) == 1
    assert factory.created[0].closed
    assert provider.worlds == []


def test_exhausted_reserve_rebuilds_only_required_active_worlds(information):
    factory = UpdatingFactory()
    provider = IncrementalBeliefProvider(factory, reserve=1)
    provider.build(information, seed=1, limit=1)[0].close()
    original = tuple(provider.worlds)
    for world in original:
        factory.failures[id(world)] = IncompatibleBelief("new public information excludes this world")
    lease = provider.build(replace(information, tick=101), seed=2, limit=1)[0]
    assert all(world.closed for world in original)
    assert len(factory.created) == 3
    assert provider.profile["invalidated"] == 2
    assert provider.profile["reserve_invalidated"] == 1
    assert provider.profile["active_worlds"] == 1
    assert provider.profile["reserve_worlds"] == 0
    assert provider.profile["rebuild_avoided"] == 0
    lease.close()
    provider.close()
    assert all(world.closed for world in factory.created)
    assert provider.profile["active_worlds"] == provider.profile["reserve_worlds"] == 0


def test_reserve_exhaustion_preserves_survivor_when_replenishment_fails(information):
    factory = UpdatingFactory()
    provider = IncrementalBeliefProvider(factory, reserve=1, attempts=3)
    for lease in provider.build(information, seed=1, limit=2):
        lease.close()
    survivor, excluded_active, excluded_reserve = provider.worlds
    for world in (excluded_active, excluded_reserve):
        factory.failures[id(world)] = IncompatibleBelief("sample excluded")

    class NoNewWorlds:
        update = factory.update

        def __call__(self, information, **kwargs):
            raise ValueError("sample rejected")

    provider.factory = NoNewWorlds()
    leases = provider.build(replace(information, tick=101), seed=2, limit=2)
    assert len(leases) == 1 and leases[0].world is survivor
    assert excluded_active.closed and excluded_reserve.closed
    assert provider.profile["active_worlds"] == 1
    assert provider.profile["reserve_worlds"] == 0
    leases[0].close()
    provider.close()


def test_reserve_does_not_change_search_when_active_world_survives(information):
    config = SearchConfig(particles=1)
    providers = [IncrementalBeliefProvider(UpdatingFactory(), reserve=reserve) for reserve in (0, 1)]
    try:
        for tick in (100, 200):
            results = [InformationSetSearch(provider, ToyInference(), config=config).run(
                replace(information, tick=tick), seed=7, training=True) for provider in providers]
            assert results[0].visits == results[1].visits
            assert results[0].selected == results[1].selected
            assert all(sum(result.visits.values()) == 32 for result in results)
        assert providers[1].profile["reserve_worlds"] == 1
        assert providers[1].worlds[1].action is None
    finally:
        for provider in providers:
            provider.close()


@pytest.mark.parametrize("reserve", [-1, 8, 1.5, True])
def test_invalid_reserve_is_rejected(reserve):
    with pytest.raises(ValueError, match="reserve"):
        IncrementalBeliefProvider(UpdatingFactory(), reserve=reserve)


def test_active_and_reserve_worlds_share_the_eight_world_limit(information):
    factory = UpdatingFactory()
    provider = IncrementalBeliefProvider(factory, reserve=1)
    with pytest.raises(ValueError, match="at most eight"):
        provider.build(information, seed=1, limit=8)
    assert factory.created == []


def test_owner_isolation_and_overlapping_lease_rejected(information):
    provider = IncrementalBeliefProvider(UpdatingFactory())
    lease = provider.build(information, seed=1, limit=1)[0]
    with pytest.raises(ValueError, match="active search"):
        provider.build(information, seed=2, limit=1)
    lease.close()
    with pytest.raises(ValueError, match="between owners"):
        provider.build(replace(information, owner=1), seed=2, limit=1)
    provider.close()


def test_native_incremental_update_replays_only_new_history(information):
    previous = replace(information, own_history=("initial", "old"))
    current = replace(previous, tick=105, own_history=(*previous.own_history, "new"))
    calls = []
    world = SimpleNamespace(root_information=lambda: previous, runtime=object(), trace=[],
                            restore_root=lambda: calls.append("restore"),
                            replace_root=lambda: calls.append("replace"))
    factory = NativeReplayFactory([30090], {}, {}, object)
    factory._replay = lambda runtime, information, **options: calls.append(options)
    factory.update(world, current)
    assert calls == ["restore", {"own_start": 2, "public_start": 0}, "replace"]
    calls.clear()
    with pytest.raises(IncompatibleBelief, match="extend"):
        factory.update(world, replace(current, own_history=("different",)))
    assert calls == []


def test_native_incremental_update_keeps_identical_clean_root_without_recapture(information):
    calls = []
    world = SimpleNamespace(root_information=lambda: information, runtime=object(), trace=[],
                            restore_root=lambda: calls.append("restore"),
                            replace_root=lambda: calls.append("replace"))
    factory = NativeReplayFactory([30090], {}, {}, object)
    factory._replay = lambda *arguments, **options: calls.append("replay")
    factory.update(world, information)
    assert calls == []


def test_native_incremental_same_tick_new_private_information_is_still_checked(information):
    calls = []
    world = SimpleNamespace(root_information=lambda: information, runtime=object(), trace=[],
                            restore_root=lambda: calls.append("restore"),
                            replace_root=lambda: calls.append("replace"))
    factory = NativeReplayFactory([30090], {}, {}, object)
    factory._replay = lambda *arguments, **options: calls.append("replay")
    factory.update(world, replace(information, own_history=("newly-visible-draw",)))
    assert calls == ["restore", "replay", "replace"]


def test_partial_compatible_pool_survives_failed_replenishment(information):
    factory = UpdatingFactory()
    provider = IncrementalBeliefProvider(factory, attempts=1)
    provider.build(information, seed=1, limit=1)[0].close()

    class NoNewWorlds:
        update = factory.update

        def __call__(self, information, **kwargs):
            raise ValueError("sample rejected")

    provider.factory = NoNewWorlds()
    leases = provider.build(replace(information, tick=101), seed=2, limit=2)
    assert len(leases) == 1
    leases[0].close()
    provider.close()
