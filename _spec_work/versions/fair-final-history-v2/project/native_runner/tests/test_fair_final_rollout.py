from copy import deepcopy
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_final_runtime import TickEnvironment
from native_runner.training.fair_final import rollout as rollout_module
from native_runner.training.fair_final.policy import FullAction, LegalPrefixes
from native_runner.training.fair_final.rollout import FinalRollout, RolloutSnapshot
from native_runner.training.fair_final.timing import DecisionClock


class MemorySnapshot:
    def __init__(self, runtime):
        self.runtime = runtime
        self.environment = deepcopy(runtime.environment.__dict__)
        self.clock = runtime.clock.capture()
        self.frozen = deepcopy(runtime.frozen)
        self.closed = False

    def restore(self):
        if self.closed:
            raise ValueError("released snapshot")
        self.runtime.environment.__dict__ = deepcopy(self.environment)
        self.runtime.clock = DecisionClock.restore(self.clock)
        self.runtime.frozen = deepcopy(self.frozen)
        self.runtime.failed = self.runtime.ended = self.runtime.truncated = False

    def close(self):
        self.closed = True


class MemoryTensorizer:
    def __init__(self, owner):
        self.actor_owner = owner
        self._opponent_revealed = set()
        self._opponent_evolution_revealed = set()
        self._ability_state_by_source_entity = {}
        self._previous_action = torch.zeros(2)
        self._episode_id = "test"
        self._episode_start_tick = 100
        self._last_tensorized_tick = None
        self.tracker = {"seen": set()}
        for name in RolloutSnapshot.tensorizer_caches:
            setattr(self, name, {})

    def start_episode(self, observation, **kwargs):
        pass

    def tensorize(self, observation, **kwargs):
        self._last_tensorized_tick = observation.tick
        self.tracker["seen"].add(observation.tick)
        return {"tick": observation.tick, "owner": self.actor_owner,
            "seen": sorted(self.tracker["seen"]), "previous": self._previous_action.clone()}


@pytest.fixture
def rollout(monkeypatch):
    environment = TickEnvironment()
    environment.warmup_ticks = environment.current_tick
    environment.episode_config = object()
    original_observe = environment.observe_dual

    def observe(**kwargs):
        observations = original_observe(**kwargs)
        for observation in observations.values():
            observation.players = [SimpleNamespace(owner=owner, elixir_exact=6.) for owner in (0, 1)]
            observation.events = ()
        return observations

    environment.observe_dual = observe
    monkeypatch.setattr(rollout_module, "build_fair_tensorizer",
        lambda observation: MemoryTensorizer(observation.owner))
    monkeypatch.setattr(rollout_module, "canonicalize_event_observation", lambda frame: frame)
    monkeypatch.setattr(rollout_module, "legal_prefixes", lambda *args, **kwargs:
        LegalPrefixes([11], torch.ones(1, 1, 31, dtype=torch.bool)))
    result = FinalRollout(environment, catalog=object(), execution_seed=19)
    result.runtime.snapshot = lambda factory: MemorySnapshot(result.runtime)
    return result


@pytest.mark.parametrize("frozen", [False, True])
def test_rollout_restores_trackers_frames_and_independent_rng(rollout, frozen):
    if frozen:
        rollout.freeze_due_information()
    assert not torch.equal(rollout.generators[0].get_state(), rollout.generators[1].get_state())
    with rollout.snapshot(None) as snapshot:
        baseline = {owner: info.sha256 for owner, info in rollout.freeze_due_information().items()}
        expected_random = {owner: torch.rand(10, generator=generator)
            for owner, generator in rollout.generators.items()}
        rollout.commit_simultaneous({0: FullAction(11, 0, 1), 1: FullAction()})
        rollout.advance()
        rollout.advance()
        rollout.freeze_due_information()
        for tensorizer in rollout.tensorizers.values():
            tensorizer._previous_action.fill_(7)
            tensorizer._opponent_revealed.add(99)
            tensorizer._placement_tensor_cache[99] = object()
        rollout.runtime.failed = True
        for attempt in range(2):
            snapshot.restore()
            assert not rollout.runtime.failed
            assert rollout.runtime.clock.tick == 100
            assert rollout.runtime.clock.next_identity == 0
            assert rollout.runtime.clock.evidence == []
            assert {owner: info.sha256 for owner, info in rollout.freeze_due_information().items()} == baseline
            for owner, tensorizer in rollout.tensorizers.items():
                assert tensorizer.tracker["seen"] == {100}
                assert tensorizer._opponent_revealed == set()
                assert not tensorizer._placement_tensor_cache
                torch.testing.assert_close(torch.rand(10, generator=rollout.generators[owner]), expected_random[owner])
    assert snapshot.native.closed


def test_pending_rollout_snapshot_replays_same_next_decision(rollout):
    rollout.commit_simultaneous({0: FullAction(11, 0, 2), 1: FullAction()})
    rollout.advance()
    with rollout.snapshot(None) as snapshot:
        results = []
        for attempt in range(2):
            for step in range(3):
                rollout.advance()
            results.append(({owner: info.sha256 for owner, info in rollout.freeze_due_information().items()},
                deepcopy(rollout.runtime.clock.evidence)))
            snapshot.restore()
        assert results[0] == results[1]


def test_rollout_rejects_replaced_components_and_released_snapshots(rollout):
    snapshot = rollout.snapshot(None)
    original = rollout.generators[0]
    rollout.generators[0] = torch.Generator()
    with pytest.raises(ValueError, match="component identity"):
        snapshot.restore()
    assert rollout.runtime.failed
    rollout.generators[0] = original
    snapshot.restore()
    snapshot.close()
    with pytest.raises(ValueError, match="released"):
        snapshot.restore()
    assert rollout.runtime.failed


def test_partial_tensorization_fails_closed_until_full_snapshot_restore(rollout):
    original = rollout.tensorizers[1].tensorize

    def fail(observation, **kwargs):
        raise ValueError("invalid semantic frame")

    with rollout.snapshot(None) as snapshot:
        rollout.tensorizers[1].tensorize = fail
        with pytest.raises(ValueError, match="invalid semantic"):
            rollout.freeze_due_information()
        assert rollout.runtime.failed and rollout.information == {}
        with pytest.raises(Exception, match="explicit checkpoint recovery"):
            rollout.freeze_due_information()
        rollout.tensorizers[1].tensorize = original
        snapshot.restore()
        assert set(rollout.freeze_due_information()) == {0, 1}
