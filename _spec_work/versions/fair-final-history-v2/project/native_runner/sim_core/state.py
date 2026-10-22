"""Mutable battle state for the P1 simulator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import ruleset as R


@dataclass
class Buff:
    """Active debuff on an entity. Freeze = full stop; slow = 0.7x."""
    name: str
    remaining_ms: int
    speed_factor: float
    hit_factor: float

    @property
    def frozen(self) -> bool:
        return self.speed_factor <= 0.0


@dataclass
class Entity:
    entity_id: int
    owner: int
    card: R.CardSpec
    x: int
    y: int
    hitpoints: int

    deploy_remaining_ms: int = 1000
    lifetime_remaining_ms: int | None = None  # buildings only
    target_entity_id: int | None = None
    attack_cooldown_ms: int = 0  # counts down between hits
    windup_remaining_ms: int | None = None  # first-attack LoadTime after acquire
    buffs: list[Buff] = field(default_factory=list)

    @property
    def is_building(self) -> bool:
        return self.card.kind == "building"

    @property
    def airborne(self) -> bool:
        return False  # subset has no air units

    @property
    def deploying(self) -> bool:
        return self.deploy_remaining_ms > 0

    def effective_speed_units_per_tick(self) -> float:
        factor = 1.0
        for buff in self.buffs:
            factor = min(factor, buff.speed_factor)
        return 0.0 if self.is_building else R.units_per_tick(self.card.speed, factor)

    def attack_interval_ms(self) -> int:
        factor = 1.0
        for buff in self.buffs:
            factor = min(factor, buff.hit_factor)
        if factor <= 0.0:
            return 0  # frozen: no attacks
        return int(self.card.hit_speed_ms / factor)

    def frozen(self) -> bool:
        return any(buff.frozen for buff in self.buffs)


@dataclass
class Tower:
    entity_id: int
    owner: int
    tower_kind: str  # "king" | "princess_left" | "princess_right"
    spec: R.TowerSpec
    x: int
    y: int
    hitpoints: int
    active: bool = True  # king towers start inactive
    attack_cooldown_ms: int = 0
    windup_remaining_ms: int | None = None
    target_entity_id: int | None = None
    buffs: list[Buff] = field(default_factory=list)

    @property
    def is_king(self) -> bool:
        return self.tower_kind == "king"

    def attack_interval_ms(self) -> int:
        return self.spec.hit_speed_ms

    def frozen(self) -> bool:
        return False  # towers are freeze-immune in this subset (NoEffectToCrownTowers)


@dataclass
class Projectile:
    projectile_id: int
    owner: int
    x: float
    y: float
    spec: R.ProjectileSpec | None = None  # None for log rolls
    target_entity_id: int | None = None  # homing single-target
    target_tower_id: int | None = None
    dest_x: float | None = None  # point target (fireball / dead-target fallback)
    dest_y: float | None = None
    speed_per_tick: float = 0.0
    damage: int = 0
    # Log roll state
    is_roll: bool = False
    dir_x: float = 0.0
    dir_y: float = 1.0
    remaining_range: float = 0.0
    width: int = 0
    hit_entity_ids: set[int] = field(default_factory=set)
    hit_tower_ids: set[int] = field(default_factory=set)


@dataclass
class PlayerState:
    owner: int
    deck: tuple[int, ...]  # 8 card ids in order
    elixir_raw: float = float(R.STARTING_ELIXIR_RAW)
    hand: list[int] = field(default_factory=list)  # 4 card ids
    queue: list[int] = field(default_factory=list)  # remaining cycle order
    crowns: int = 0


@dataclass
class GameState:
    seed: int
    tick: int = 0
    next_entity_id: int = 1
    players: dict[int, PlayerState] = field(default_factory=dict)
    towers: dict[int, Tower] = field(default_factory=dict)
    entities: dict[int, Entity] = field(default_factory=dict)
    projectiles: dict[int, Projectile] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    # Terminal bookkeeping (wire-spec.md section 6)
    ended: bool = False
    winner: int | None = None
    terminal_reason: str | None = None
    terminal_tick: int | None = None

    def alloc_entity_id(self) -> int:
        value = self.next_entity_id
        self.next_entity_id += 1
        return value

    def log_event(self, kind: str, **data: Any) -> None:
        self.events.append({"tick": self.tick, "kind": kind, **data})

    # -------------------------------------------------------------- queries --
    def enemy_entities(self, owner: int) -> list[Entity]:
        return [e for e in self.entities.values() if e.owner != owner and e.hitpoints > 0]

    def enemy_towers(self, owner: int) -> list[Tower]:
        return [t for t in self.towers.values() if t.owner != owner and t.hitpoints > 0]

    def towers_of(self, owner: int) -> list[Tower]:
        return [t for t in self.towers.values() if t.owner == owner and t.hitpoints > 0]
