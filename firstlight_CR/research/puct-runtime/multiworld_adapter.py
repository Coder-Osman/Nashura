"""Search adapter that keeps every hidden-state particle in its own native world.

Native snapshot handles are process and match-generation local.  This adapter
therefore never copies a handle between worlds.  A root has eight separately
captured particle roots; descendant states carry the world that produced them.
PUCT shares root statistics while retaining particle-specific native branches.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Callable, Mapping

from particle_pool import HiddenParticle, ParticleSet
from native_adapter import BattleEnvSidecar
from tree_search import SearchState, Transition, state_digest


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


@dataclass
class WorldBinding:
    """All mutable components associated with one independent native world."""

    world_id: str
    native: Any
    capture: Callable[[], SearchState]
    restore_sidecar: Callable[[SearchState, Mapping[str, Any] | None], None]
    apply: Callable[[str], None]
    advance: Callable[[int, float], tuple[float, int, float]]
    policy_input_sha256: Callable[[], str]
    public_history_valid: Callable[[HiddenParticle], bool]
    battle_sidecar: BattleEnvSidecar | None = None


class MultiWorldSnapshotSearchAdapter:
    """A strict adapter for PUCT with independently replayed hidden worlds."""

    def __init__(self, worlds: Mapping[str, WorldBinding]):
        if not worlds or set(worlds) != {binding.world_id for binding in worlds.values()}:
            raise ValueError("world IDs must be unique and map to their bindings")
        self.worlds = dict(worlds)
        self._sidecars: dict[tuple[str, int], dict[str, Any]] = {}
        self._owned: set[tuple[str, int]] = set()
        self._active_world: str | None = None
        self._active: SearchState | None = None

    @staticmethod
    def _local_snapshot(native_snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        nested = native_snapshot.get("snapshot")
        return nested if isinstance(nested, Mapping) else native_snapshot

    @staticmethod
    def _with_world_binding(snapshot: Mapping[str, Any], world_id: str) -> Mapping[str, Any]:
        """Record the process-local owner next to the opaque native handle.

        Snapshot handles are only unique inside a native world.  The marker is
        task metadata, never sent back to native; restore still passes only the
        handle.  It makes an accidental cross-world handle reuse fail before
        the handle table is touched.
        """
        local = MultiWorldSnapshotSearchAdapter._local_snapshot(snapshot)
        if "_tree_search_world_id" in local:
            raise ValueError("native snapshot uses a reserved search metadata field")
        annotated = {**dict(local), "_tree_search_world_id": world_id}
        if local is snapshot:
            return annotated
        return {**dict(snapshot), "snapshot": annotated}

    def _native_digest(self, world: WorldBinding) -> str:
        result = world.native._request("state-digest")
        digest = result.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("native state digest missing")
        return digest

    def _capture(self, world: WorldBinding, *, particles: tuple[Mapping[str, Any], ...] | None = None) -> SearchState:
        frame = deepcopy(world.capture())
        if frame.native_snapshot:
            raise ValueError("world capture must not supply a native snapshot")
        if frame.observation.get("truncated") is not False:
            raise ValueError("search requires an untruncated learner observation")
        sidecar = world.battle_sidecar.capture() if world.battle_sidecar is not None else None
        snapshot = self._with_world_binding(world.native.create_snapshot(), world.world_id)
        local = self._local_snapshot(snapshot)
        handle = local.get("handle")
        if type(handle) is not int or handle <= 0:
            raise ValueError("native snapshot handle missing")
        key = (world.world_id, handle)
        if key in self._owned:
            raise ValueError("native snapshot handle is already owned")
        self._owned.add(key)
        if sidecar is not None:
            self._sidecars[key] = sidecar
        try:
            native_sha = self._native_digest(world)
            observed = world.native.observe()
            if local.get("tick") != observed.get("tick") or frame.observation.get("tick") != local.get("tick"):
                raise ValueError("world observation and native snapshot ticks differ")
            payload = {
                "world_id": world.world_id,
                "snapshot": deepcopy(dict(snapshot)),
                "state_sha256": native_sha,
            }
            if particles is not None:
                payload["particle_roots"] = [dict(item) for item in particles]
            state = replace(frame, native_snapshot=payload,
                            hidden_particles=(particles if particles is not None else frame.hidden_particles))
            state_digest(state)
            self._active_world, self._active = world.world_id, state
            return state
        except BaseException:
            self._release_key(key)
            raise

    def create_root_state(self, particle_set: ParticleSet) -> SearchState:
        """Capture every conditioned root while each world is at its verified replay state."""

        if set(particle.world_id for particle in particle_set.particles) != set(self.worlds):
            raise ValueError("particle worlds do not match the configured native worlds")
        payloads = particle_set.to_search_payloads()
        roots: list[dict[str, Any]] = []
        captured: list[SearchState] = []
        baseline_observation: Mapping[str, Any] | None = None
        try:
            for particle in particle_set.particles:
                world = self.worlds[particle.world_id]
                if world.policy_input_sha256() != particle.policy_input_sha256:
                    raise ValueError("conditioned world changed the learner policy input")
                if not world.public_history_valid(particle):
                    raise ValueError("conditioned world contradicts the public replay history")
                state = self._capture(world)
                captured.append(state)
                if baseline_observation is None:
                    baseline_observation = state.observation
                elif state.observation != baseline_observation:
                    raise ValueError("conditioned worlds changed the learner fair observation")
                local = self._local_snapshot(state.native_snapshot)
                roots.append({"particle_id": particle.particle_id, "world_id": particle.world_id,
                              "snapshot": deepcopy(dict(local)),
                              "state_sha256": particle.native_state_sha256})
            anchor = captured[0]
            root = replace(anchor, native_snapshot={
                **deepcopy(dict(anchor.native_snapshot)), "particle_roots": roots,
            }, hidden_particles=payloads)
            self._active_world, self._active = particle_set.particles[0].world_id, root
            state_digest(root)
            return root
        except BaseException:
            for state in captured:
                self.release_search_state(state)
            raise

    def _restore(self, state: SearchState, *, world_id: str, snapshot: Mapping[str, Any],
                 expected_sha: str, particle: Mapping[str, Any] | None) -> None:
        world = self.worlds.get(world_id)
        if world is None:
            raise ValueError("search state references an unknown native world")
        handle = self._local_snapshot(snapshot).get("handle")
        key = (world_id, handle)
        if key not in self._owned:
            raise ValueError("native snapshot is not owned by this multiworld search")
        if self._local_snapshot(snapshot).get("_tree_search_world_id") != world_id:
            raise ValueError("native snapshot belongs to a different search world")
        if particle is not None and particle not in state.hidden_particles:
            raise ValueError("hidden particle is not part of the saved belief")
        world.native.clear_replay_schedule()
        world.native.restore(handle)
        if self._native_digest(world) != expected_sha:
            raise ValueError("native snapshot digest changed after restore")
        if world.battle_sidecar is not None:
            world.battle_sidecar.restore(self._sidecars[key])
        world.restore_sidecar(state, particle)
        for entry in state.external_queue:
            if entry.get("kind") == "card":
                world.native.schedule_replay_card_at_tick(**{key: entry[key] for key in
                    ("owner", "card_id", "x", "y", "execute_tick")})
            elif entry.get("kind") == "ability":
                world.native.schedule_replay_ability_at_tick(**{key: entry[key] for key in
                    ("owner", "ability_name_hints", "execute_tick")})
            else:
                raise ValueError("unsupported external queue entry")
        self._active_world, self._active = world_id, state

    def restore_search_state(self, state: SearchState,
                             particle: Mapping[str, Any] | None = None) -> None:
        if particle is None:
            world_id = state.native_snapshot.get("world_id")
            if not isinstance(world_id, str):
                raise ValueError("search state has no native world")
            self._restore(state, world_id=world_id, snapshot=state.native_snapshot,
                          expected_sha=str(state.native_snapshot.get("state_sha256")), particle=None)
            return
        particle_id = particle.get("particle_id")
        roots = state.native_snapshot.get("particle_roots")
        if not isinstance(particle_id, str) or not isinstance(roots, list):
            raise ValueError("only a root state can restore a hidden particle")
        match = [entry for entry in roots if entry.get("particle_id") == particle_id]
        if len(match) != 1:
            raise ValueError("root has no snapshot for this hidden particle")
        root = match[0]
        self._restore(state, world_id=str(root.get("world_id")), snapshot=root.get("snapshot", {}),
                      expected_sha=str(root.get("state_sha256")), particle=particle)

    def validate_particles(self, state: SearchState) -> None:
        roots = state.native_snapshot.get("particle_roots")
        if not isinstance(roots, list) or len(state.hidden_particles) != 8:
            raise ValueError("multiworld validation requires an eight-particle root")
        seen: set[str] = set()
        try:
            for payload in state.hidden_particles:
                particle = HiddenParticle(**{key: payload[key] for key in HiddenParticle.__dataclass_fields__})
                self.restore_search_state(state, payload)
                world = self.worlds[particle.world_id]
                if world.policy_input_sha256() != particle.policy_input_sha256:
                    raise ValueError("hidden particle changed the learner policy input")
                if not world.public_history_valid(particle):
                    raise ValueError("hidden particle contradicts the public history")
                digest = self._native_digest(world)
                if digest != particle.native_state_sha256 or digest in seen:
                    raise ValueError("hidden particle native state is invalid or duplicated")
                seen.add(digest)
        finally:
            self.restore_search_state(state)

    def save_search_state(self) -> SearchState:
        if self._active_world is None:
            raise ValueError("no active world is available for a search snapshot")
        return self._capture(self.worlds[self._active_world])

    def apply_action(self, action: str) -> None:
        if self._active_world is None:
            raise ValueError("apply action requires a restored search state")
        self._active = None
        self.worlds[self._active_world].apply(action)

    def advance_until_decision(self, max_decision_points: int,
                               max_native_seconds: float) -> Transition:
        if self._active_world is None:
            raise ValueError("advance requires a restored search state")
        world = self.worlds[self._active_world]
        reward, decisions, seconds = world.advance(max_decision_points, max_native_seconds)
        return Transition(self.save_search_state(), reward, decisions, seconds)

    def fingerprint(self) -> str:
        if self._active is None or self._active_world is None:
            raise ValueError("fingerprint requires a restored search state")
        expected_world = self._active.native_snapshot.get("world_id")
        if expected_world != self._active_world:
            raise ValueError("fingerprint requires the anchor state, not a particle restoration")
        world = self.worlds[self._active_world]
        if self._native_digest(world) != self._active.native_snapshot.get("state_sha256"):
            raise ValueError("native state diverged from saved search state")
        return _digest({"native": self._native_digest(world), "search": state_digest(self._active)})

    def _release_key(self, key: tuple[str, int]) -> None:
        if key in self._owned:
            self.worlds[key[0]].native.release_snapshot(key[1])
            self._owned.remove(key)
            self._sidecars.pop(key, None)

    def release_search_state(self, state: SearchState) -> None:
        locations: list[tuple[str, Mapping[str, Any]]] = []
        world_id = state.native_snapshot.get("world_id")
        if isinstance(world_id, str):
            locations.append((world_id, state.native_snapshot))
        roots = state.native_snapshot.get("particle_roots")
        if isinstance(roots, list):
            locations.extend((str(entry.get("world_id")), entry.get("snapshot", {})) for entry in roots)
        for candidate_world, snapshot in locations:
            handle = self._local_snapshot(snapshot).get("handle")
            if type(handle) is int:
                self._release_key((candidate_world, handle))
        if self._active is state:
            self._active, self._active_world = None, None

    @property
    def owned_snapshot_count(self) -> int:
        return len(self._owned)
