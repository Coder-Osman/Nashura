from dataclasses import dataclass
from copy import deepcopy
import hashlib

import torch

from ..event_cache import canonicalize_event_observation
from ..fair_search.contracts import pack, unpack, semantic_digest
from .adapter import legal_prefixes
from .information import build_fair_tensorizer
from .native import FinalDecisionRuntime


@dataclass(frozen=True)
class DecisionInformation:
    owner: int
    tick: int
    frame: object
    legality: object

    @property
    def sha256(self):
        return semantic_digest(dict(owner=self.owner, tick=self.tick, frame=self.frame,
            card_ids=self.legality.card_ids, plays=self.legality.plays, modes=self.legality.modes))


class FinalRollout:
    def __init__(self, environment, *, catalog, execution_seed):
        if type(execution_seed) is not int or execution_seed < 0:
            raise ValueError("execution RNG requires an independent nonnegative seed")
        self.runtime = FinalDecisionRuntime(environment)
        self.catalog = catalog
        self.generators = {}
        for owner in (0, 1):
            identity = f"fair-final-execution.v1:{execution_seed}:{owner}".encode()
            seed = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")
            self.generators[owner] = torch.Generator().manual_seed(seed)
        self.tensorizers = {}
        observations = self.runtime.freeze_due_information()
        if environment.current_tick != environment.warmup_ticks or any(
                observation.events for observation in observations.values()):
            raise ValueError("fresh rollout requires the public no-action opening; use a snapshot to resume")
        for owner in (0, 1):
            observation = observations[owner]
            initial = next(player.elixir_exact for player in observation.players if player.owner == owner)
            tensorizer = build_fair_tensorizer(observation)
            tensorizer.start_episode(observation, initial_elixir={0: initial, 1: initial})
            self.tensorizers[owner] = tensorizer
        self.information = {}

    def freeze_due_information(self):
        observations = self.runtime.freeze_due_information()
        if not self.information:
            try:
                information = {}
                for owner, observation in observations.items():
                    frame = canonicalize_event_observation(self.tensorizers[owner].tensorize(observation, validate=True))
                    legality = legal_prefixes(self.runtime.environment, observation, frame, owner, catalog=self.catalog)
                    information[owner] = DecisionInformation(owner, observation.tick, frame, legality)
                self.information = information
            except Exception:
                self.runtime.failed = True
                raise
        return {owner: DecisionInformation(info.owner, info.tick, unpack(pack(info.frame)), deepcopy(info.legality))
            for owner, info in self.information.items()}

    def commit_simultaneous(self, actions):
        information = self.freeze_due_information()
        self.runtime.commit_simultaneous(actions, legal=lambda owner, action: information[owner].legality.legal(action))
        self.information = {}

    def advance(self):
        if self.information:
            raise ValueError("commit all frozen decisions before advancing")
        return self.runtime.advance()

    def snapshot(self, sidecar_factory):
        return RolloutSnapshot(self, sidecar_factory)


class RolloutSnapshot:
    schema = "fair-final-rollout-snapshot.v1"
    tensorizer_fields = ("_opponent_revealed", "_opponent_evolution_revealed",
        "_ability_state_by_source_entity", "_previous_action", "_episode_id",
        "_episode_start_tick", "_last_tensorized_tick")
    tensorizer_caches = ("_placement_tensor_cache", "_frame_tile_position_cache",
        "_frame_base_card_id_cache", "_frame_spatial_extent_cache",
        "_frame_spatial_radius_cache", "_frame_archetype_id_cache", "_frame_child_kind_cache")

    def __init__(self, rollout, sidecar_factory):
        self.rollout = rollout
        self.tensorizers = dict(rollout.tensorizers)
        self.generators = dict(rollout.generators)
        self.states = {owner: deepcopy({name: getattr(tensorizer, name)
            for name in (*self.tensorizer_fields, "tracker")})
            for owner, tensorizer in self.tensorizers.items()}
        self.rng_states = {owner: generator.get_state().clone()
            for owner, generator in self.generators.items()}
        self.information = self._copy_information(rollout.information)
        self.native = rollout.runtime.snapshot(sidecar_factory)

    @staticmethod
    def _copy_information(information):
        return {owner: DecisionInformation(info.owner, info.tick, unpack(pack(info.frame)),
            deepcopy(info.legality)) for owner, info in information.items()}

    def restore(self):
        rollout = self.rollout
        try:
            if (rollout.runtime is not self.native.runtime
                    or set(rollout.tensorizers) != set(self.tensorizers)
                    or set(rollout.generators) != set(self.generators)
                    or any(rollout.tensorizers[owner] is not tensorizer
                           for owner, tensorizer in self.tensorizers.items())
                    or any(rollout.generators[owner] is not generator
                           for owner, generator in self.generators.items())):
                raise ValueError("rollout snapshot component identity changed")
            states = deepcopy(self.states)
            information = self._copy_information(self.information)
            self.native.restore()
            for owner, tensorizer in rollout.tensorizers.items():
                for name, value in states[owner].items():
                    setattr(tensorizer, name, value)
                for name in self.tensorizer_caches:
                    getattr(tensorizer, name).clear()
                rollout.generators[owner].set_state(self.rng_states[owner])
            rollout.information = information
        except Exception:
            rollout.runtime.failed = True
            raise

    def close(self):
        self.native.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
