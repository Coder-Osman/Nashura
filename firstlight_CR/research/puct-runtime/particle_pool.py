"""Auditable conditional-replay particles for the Hog tree-search task.

The native runner exposes one match RNG seed, rather than independent hidden
hand seeds.  A particle therefore represents a separately replayed legal
world, selected only when it has the same public history and the exact V4
input delivered to the learner.  Raw opponent cards are deliberately never
written into a teacher record; only a one-way hand fingerprint is retained.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping, Protocol, Sequence


PARTICLE_SCHEMA = "conditional-hidden-particle.v1"
PUBLIC_HISTORY_SCHEMA = "tree-search-public-history.v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class PublicAction:
    """One observable replay command, with no private hand-slot information."""

    owner: int
    execute_tick: int
    kind: str
    card_id: int | None = None
    x: int | None = None
    y: int | None = None
    ability_name_hints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.owner not in (0, 1) or self.execute_tick < 1:
            raise ValueError("public action owner or tick is invalid")
        if self.kind == "card":
            if (not isinstance(self.card_id, int) or self.card_id <= 0 or
                    not isinstance(self.x, int) or not 0 <= self.x < 18_000 or
                    not isinstance(self.y, int) or not 0 <= self.y < 32_000 or
                    self.ability_name_hints):
                raise ValueError("public card action is incomplete")
        elif self.kind == "ability":
            if self.card_id is not None or self.x is not None or self.y is not None:
                raise ValueError("public ability action contains card fields")
        else:
            raise ValueError("unsupported public action kind")

    def schedule(self, native: Any) -> None:
        if self.kind == "card":
            native.schedule_replay_card_at_tick(
                owner=self.owner, card_id=self.card_id, x=self.x, y=self.y,
                execute_tick=self.execute_tick,
            )
        else:
            native.schedule_replay_ability_at_tick(
                owner=self.owner, ability_name_hints=self.ability_name_hints,
                execute_tick=self.execute_tick,
            )


@dataclass(frozen=True)
class PublicHistory:
    """The observable facts that candidate worlds must replay exactly."""

    match_identity_sha256: str
    target_tick: int
    actions: tuple[PublicAction, ...]
    observed_public_sha256: str

    def __post_init__(self) -> None:
        if len(self.match_identity_sha256) != 64 or len(self.observed_public_sha256) != 64:
            raise ValueError("public history SHA-256 is invalid")
        if self.target_tick < 0:
            raise ValueError("public history target tick is invalid")
        if tuple(sorted(self.actions, key=lambda action: (action.execute_tick, action.owner, action.kind))) != self.actions:
            raise ValueError("public replay actions must be sorted deterministically")
        if any(action.execute_tick > self.target_tick for action in self.actions):
            raise ValueError("public replay action is after the target tick")

    @property
    def sha256(self) -> str:
        return sha256({"schema": PUBLIC_HISTORY_SCHEMA, **asdict(self)})


@dataclass(frozen=True)
class HiddenParticle:
    """One legal hidden world.  `world_id` identifies an independent native slot."""

    particle_id: str
    world_id: str
    seed: int
    public_history_sha256: str
    policy_input_sha256: str
    hidden_hand_sha256: str
    native_state_sha256: str

    def __post_init__(self) -> None:
        if not self.particle_id or not self.world_id:
            raise ValueError("particle identity is missing")
        if not -(1 << 31) <= self.seed < (1 << 32):
            raise ValueError("particle seed does not fit the native seed range")
        for name in ("public_history_sha256", "policy_input_sha256", "hidden_hand_sha256", "native_state_sha256"):
            if len(getattr(self, name)) != 64:
                raise ValueError(f"particle {name} is not SHA-256")

    def to_dict(self) -> dict[str, Any]:
        return {"schema": PARTICLE_SCHEMA, **asdict(self)}


@dataclass(frozen=True)
class ParticleSet:
    """Exactly eight worlds consistent with one learner information set."""

    history: PublicHistory
    particles: tuple[HiddenParticle, ...]

    def __post_init__(self) -> None:
        if len(self.particles) != 8:
            raise ValueError("tree-search root requires exactly eight hidden particles")
        if len({particle.particle_id for particle in self.particles}) != 8:
            raise ValueError("hidden particle identifiers must be unique")
        if len({particle.world_id for particle in self.particles}) != 8:
            raise ValueError("each hidden particle requires an independent native world")
        if len({particle.hidden_hand_sha256 for particle in self.particles}) != 8:
            raise ValueError("hidden particles must use different opponent hands")
        if len({particle.native_state_sha256 for particle in self.particles}) != 8:
            raise ValueError("hidden particles must have distinct native states")
        if {particle.public_history_sha256 for particle in self.particles} != {self.history.sha256}:
            raise ValueError("hidden particle does not match the public action history")
        if len({particle.policy_input_sha256 for particle in self.particles}) != 1:
            raise ValueError("hidden particles changed the learner policy input")

    @property
    def policy_input_sha256(self) -> str:
        return self.particles[0].policy_input_sha256

    def to_search_payloads(self) -> tuple[dict[str, Any], ...]:
        return tuple(particle.to_dict() for particle in self.particles)

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": "conditional-hidden-particle-set.v1",
            "public_history": {
                "sha256": self.history.sha256,
                "match_identity_sha256": self.history.match_identity_sha256,
                "target_tick": self.history.target_tick,
                "observed_public_sha256": self.history.observed_public_sha256,
                "action_count": len(self.history.actions),
            },
            "policy_input_sha256": self.policy_input_sha256,
            "particles": [particle.to_dict() for particle in self.particles],
        }


@dataclass(frozen=True)
class CandidateWorld:
    """Output of one real reset plus public-history replay."""

    seed: int
    policy_input_sha256: str
    hidden_hand_sha256: str
    native_state_sha256: str
    public_history_sha256: str
    observed_public_sha256: str

    def __post_init__(self) -> None:
        for name in ("policy_input_sha256", "hidden_hand_sha256", "native_state_sha256",
                     "public_history_sha256", "observed_public_sha256"):
            if len(getattr(self, name)) != 64:
                raise ValueError(f"candidate {name} is not SHA-256")


class CandidateReplayer(Protocol):
    """A task-owned native world that can reset then replay public actions."""

    def replay(self, seed: int, history: PublicHistory) -> CandidateWorld: ...


def select_particle_set(history: PublicHistory, candidates: Iterable[CandidateWorld], *,
                        world_ids: Sequence[str]) -> ParticleSet:
    """Select eight distinct legal hidden hands without inspecting raw cards."""

    if len(world_ids) != 8 or len(set(world_ids)) != 8:
        raise ValueError("exactly eight independent world IDs are required")
    accepted = [candidate for candidate in candidates
                if candidate.public_history_sha256 == history.sha256
                and candidate.observed_public_sha256 == history.observed_public_sha256]
    groups: dict[str, list[CandidateWorld]] = {}
    for candidate in accepted:
        groups.setdefault(candidate.policy_input_sha256, []).append(candidate)
    viable: list[tuple[str, list[CandidateWorld]]] = []
    for signature, rows in groups.items():
        by_hand: dict[str, CandidateWorld] = {}
        for row in sorted(rows, key=lambda item: (item.seed, item.native_state_sha256)):
            by_hand.setdefault(row.hidden_hand_sha256, row)
        unique = list(by_hand.values())
        if len(unique) >= 8:
            viable.append((signature, unique))
    if not viable:
        raise ValueError("seed scan has no eight-particle legal information set")
    signature, rows = min(viable, key=lambda item: (min(row.seed for row in item[1]), item[0]))
    selected = sorted(rows, key=lambda item: (item.seed, item.native_state_sha256))[:8]
    return ParticleSet(
        history=history,
        particles=tuple(
            HiddenParticle(
                particle_id=f"p{index}", world_id=world_ids[index], seed=row.seed,
                public_history_sha256=history.sha256, policy_input_sha256=signature,
                hidden_hand_sha256=row.hidden_hand_sha256,
                native_state_sha256=row.native_state_sha256,
            )
            for index, row in enumerate(selected)
        ),
    )
