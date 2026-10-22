"""Static ruleset for the P1 simulator subset.

Every numeric constant carries a source citation:
- ``feat``    = native_runner/data/competitive/model_catalogs.json.gz
                card_feature_rows (already level-11 projected, integer trunc).
- ``toml``    = decoded csv_logic release inside nulls-royale.apk
                (assets/csv_logic/characters/*.toml, projectiles, globals.csv).
- ``spec``    = card_specs.json.gz categorical_features (targeting flags).
- ``battle``  = battle_env.py / match_factory.py / runtime.json constants.
Values where the two sources disagreed are marked DISCREPANCY and resolved in
favour of the competitive tables (the model's own view of the world).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# ---------------------------------------------------------------- timing ---
# battle: TICKS_PER_SECOND = 20 (contracts.py:59, battle_env.py:115)
TICK_MS = 50
TICKS_PER_SECOND = 20
# battle: cr_native_env.py:36, battle_env.py:382 — first tick a command applies
FIRST_PLAYABLE_TICK = 90
# battle: expert.py:27 / evaluate.py:78 — one decision per 5 native ticks
DECISION_TICKS = 5
# battle: match_factory.py:24-31 — 180 s regulation + 120 s overtime, then a
# finalization watchdog; the simulator always ends at or before 6000.
REGULATION_END_TICK = 3600  # runtime.json timelines.Default sections: 180 s Normal
GAMEPLAY_END_TICK = 6000  # match_factory.py NATIVE_GAMEPLAY_END_TICK

# Board is 18 x 32 tiles; world coordinates are thousandths of a tile
# (contracts.py:57-58, cr_native_env.py:3002-3003).
BOARD_WIDTH_TILES = 18
BOARD_HEIGHT_TILES = 32
WORLD_X_LIMIT = BOARD_WIDTH_TILES * 1000
WORLD_Y_LIMIT = BOARD_HEIGHT_TILES * 1000

# battle_env.py:138-145 TOWER_LAYOUT (owner 0 at the bottom).
TOWER_POSITIONS = {
    (0, "king"): (9000, 3000),
    (0, "princess_left"): (3500, 6500),
    (0, "princess_right"): (14500, 6500),
    (1, "king"): (9000, 29000),
    (1, "princess_left"): (3500, 25500),
    (1, "princess_right"): (14500, 25500),
}

# River band and the two bridges (ground units must cross via a bridge).
RIVER_Y_MIN = 15000
RIVER_Y_MAX = 17000
BRIDGE_X = (3500, 14500)
BRIDGE_HALF_WIDTH = 1000

# ---------------------------------------------------------------- elixir ---
# runtime.json timelines.Default (battle_timelines.csv, sha fc79fcc…):
# starting_elixir 6.0, rates 28 s/14 s/9.3 s per full bar over 120/120/60 s.
# Matches battle_env.py:131-135 ELIXIR_SEGMENTS.
STARTING_ELIXIR_RAW = 60_000  # fixed point 1/10000 -> 6.0
ELIXIR_CAP_RAW = 100_000
ELIXIR_SEGMENTS = (
    (0, 28_000),  # tick start, ms per full bar
    (2400, 14_000),
    (4800, 9_300),
)

# ------------------------------------------------------------------ cards ---
SUBSET_CARD_IDS = (
    26000021,  # Hog Rider
    26000038,  # Ice Golem
    26000014,  # Musketeer
    26000010,  # Skeletons
    28000000,  # Fireball
    28000011,  # The Log
    27000000,  # Cannon
    26000030,  # Ice Spirit
)


@dataclass(frozen=True)
class ProjectileSpec:
    speed: int  # toml Speed; world units per tick = speed * 50 / 60
    damage: int  # level-11 (feat or toml x 2.56 truncated)
    aoe_radius: int | None = None  # thousandths; None = single target
    hits_air: bool = True
    hits_ground: bool = True
    crown_tower_ratio: float = 1.0  # toml CrownTowerDamagePercent as a ratio
    freeze_ms: int = 0
    slow_ms: int = 0
    slow_factor: float = 1.0  # speed/hit-speed multiplier while slowed


@dataclass(frozen=True)
class CardSpec:
    card_id: int
    name: str
    kind: str  # troop | building | spell
    elixir_cost: int
    count: int = 1
    hitpoints: int | None = None
    damage: int | None = None
    hit_speed_ms: int | None = None
    load_time_ms: int = 0  # first-attack wind-up (toml LoadTime)
    deploy_time_ms: int = 1000
    lifetime_ms: int | None = None  # buildings (toml LifeTime)
    speed: int = 0  # toml Speed; 0 = immobile. units_per_tick = speed * 5 / 6
    range_thousandths: int = 0
    sight_thousandths: int = 5500
    collision_radius: int = 500
    attacks_air: bool = False
    attacks_ground: bool = True
    targets_only_buildings: bool = False  # spec categorical target_only_buildings
    kamikaze: bool = False  # dies on its own hit (toml Kamikaze)
    projectile: ProjectileSpec | None = None
    death_aoe_radius: int | None = None  # Ice Golem death nova (toml)
    death_aoe_damage: int | None = None
    death_aoe_slow_ms: int = 0
    death_aoe_slow_factor: float = 1.0
    spell_travel: ProjectileSpec | None = None  # spells: cast-to-target flight
    # The Log: forward rolling sweep
    roll_range: int | None = None
    roll_width: int | None = None
    formation: tuple[tuple[int, int], ...] = ((0, 0),)


_MUSKETEER_PROJECTILE = ProjectileSpec(speed=1000, damage=217)  # toml MusketeerProjectile
_CANNON_PROJECTILE = ProjectileSpec(speed=1000, damage=202)  # toml TowerCannonball
_ICE_SPIRIT_PROJECTILE = ProjectileSpec(  # toml IceSpiritsProjectile
    speed=400,
    damage=110,
    aoe_radius=1500,
    freeze_ms=1100,  # TargetBuff Freeze, BuffTime 1100
)
_FIREBALL = ProjectileSpec(  # toml PROJECTILE.FireballSpell
    speed=600,
    damage=688,
    aoe_radius=2500,
    crown_tower_ratio=0.25,  # CrownTowerDamagePercent = -75
)
# toml PROJECTILE.LogProjectileRolling: range 10100, width radius 1950,
# speed 200, damage 105 x2.56 -> 268, tower -87%.
_LOG_ROLL = dict(damage=268, roll_range=10100, roll_width=1950)

# Formation approximations for multi-unit cards (spawn_groups data not
# present in this release; standard triangle/row offsets, flagged in wire-spec).
_SKELETON_FORMATION = ((0, -500), (-450, 350), (450, 350))

CARDS: Mapping[int, CardSpec] = {
    26000021: CardSpec(
        card_id=26000021, name="Hog Rider", kind="troop", elixir_cost=4,
        hitpoints=1697, damage=317, hit_speed_ms=1600, load_time_ms=1000,
        speed=120, range_thousandths=800, sight_thousandths=9500,
        collision_radius=600, targets_only_buildings=True,
    ),
    26000038: CardSpec(
        card_id=26000038, name="Ice Golem", kind="troop", elixir_cost=2,
        hitpoints=1315, damage=84, hit_speed_ms=2500, load_time_ms=1500,
        speed=45, range_thousandths=750, sight_thousandths=7000,
        collision_radius=700, attacks_air=True, targets_only_buildings=True,
        death_aoe_radius=2000, death_aoe_damage=84,
        death_aoe_slow_ms=2000, death_aoe_slow_factor=0.7,  # IceWizardSlowDown -30%
    ),
    26000014: CardSpec(
        card_id=26000014, name="Musketeer", kind="troop", elixir_cost=4,
        hitpoints=721, damage=217, hit_speed_ms=1000, load_time_ms=300,
        speed=60, range_thousandths=6000, sight_thousandths=6000,
        collision_radius=500, attacks_air=True, projectile=_MUSKETEER_PROJECTILE,
    ),
    26000010: CardSpec(
        card_id=26000010, name="Skeletons", kind="troop", elixir_cost=1,
        count=3, hitpoints=81, damage=81, hit_speed_ms=1100, load_time_ms=600,
        speed=90, range_thousandths=500, sight_thousandths=5500,
        collision_radius=500, formation=_SKELETON_FORMATION,
    ),
    28000000: CardSpec(
        card_id=28000000, name="Fireball", kind="spell", elixir_cost=4,
        spell_travel=_FIREBALL,
    ),
    28000011: CardSpec(
        card_id=28000011, name="The Log", kind="spell", elixir_cost=2,
        roll_range=_LOG_ROLL["roll_range"], roll_width=_LOG_ROLL["roll_width"],
    ),
    27000000: CardSpec(
        card_id=27000000, name="Cannon", kind="building", elixir_cost=3,
        hitpoints=824, damage=202, hit_speed_ms=1000, load_time_ms=100,
        deploy_time_ms=1000, lifetime_ms=30000,
        range_thousandths=5500, sight_thousandths=5500, collision_radius=600,
        projectile=_CANNON_PROJECTILE,
    ),
    26000030: CardSpec(
        card_id=26000030, name="Ice Spirit", kind="troop", elixir_cost=1,
        # DISCREPANCY: toml Hitpoints 90 vs competitive 84 -> 215 at level 11;
        # competitive wins (it is what the trained model consumes).
        hitpoints=215, damage=110, hit_speed_ms=300, load_time_ms=100,
        speed=120, range_thousandths=2500, sight_thousandths=5500,
        collision_radius=400, attacks_air=True, kamikaze=True,
        projectile=_ICE_SPIRIT_PROJECTILE,
    ),
}

# The Log's rolling damage is carried on the card for simplicity.
LOG_ROLL_DAMAGE = _LOG_ROLL["damage"]
LOG_ROLL_SPEED = 200
LOG_TOWER_RATIO = 0.13  # toml LogProjectileRolling CrownTowerDamagePercent -87


# ----------------------------------------------------------------- towers ---
# globals.csv: HITPOINT/DAMAGE_INCREASE_PERCENT_PER_TOWER_LEVEL = 8; towers are
# level 11 in tournament mode -> base * 1.08^10 = 2.158925, truncated.
_TOWER_LEVEL_11_MULTIPLIER = 1.08 ** 10


def _tower_value(base: int) -> int:
    return int(base * _TOWER_LEVEL_11_MULTIPLIER)  # trunc toward zero


@dataclass(frozen=True)
class TowerSpec:
    tower_kind: str
    hitpoints: int
    damage: int
    hit_speed_ms: int
    load_time_ms: int
    range_thousandths: int
    sight_thousandths: int
    collision_radius: int
    projectile_speed: int


PRINCESS_TOWER = TowerSpec(
    tower_kind="princess",
    hitpoints=_tower_value(1400),  # toml BUILDING.PrincessTower
    damage=_tower_value(50),  # toml TowerPrincessProjectile (DamageScalingMode scales w/ level)
    hit_speed_ms=800,
    load_time_ms=0,
    range_thousandths=7500,
    sight_thousandths=7500 + 2000,  # globals.csv EXTRA_SIGHT_RANGE_TO_CROWN_TOWERS
    collision_radius=1000,
    projectile_speed=600,
)

KING_TOWER = TowerSpec(
    tower_kind="king",
    hitpoints=_tower_value(2400),  # toml BUILDING.KingTower
    damage=_tower_value(50),  # toml KingProjectile
    hit_speed_ms=1000,
    load_time_ms=500,
    range_thousandths=7000,
    sight_thousandths=7000 + 2000,
    collision_radius=1400,
    projectile_speed=1000,
)


def elixir_rate_raw_per_tick(start_tick: int) -> float:
    """Fixed-point elixir gained per tick at `start_tick` (segment lookup)."""
    full_bar_ms = ELIXIR_SEGMENTS[-1][1]
    for seg_start, seg_ms in ELIXIR_SEGMENTS:
        if start_tick >= seg_start:
            full_bar_ms = seg_ms
    return (ELIXIR_CAP_RAW * TICK_MS) / full_bar_ms


def units_per_tick(speed: int, slow_factor: float = 1.0) -> float:
    """toml Speed -> world-thousandths per 50 ms tick (Speed 60 = 1 tile/s)."""
    return speed * 5.0 / 6.0 * slow_factor
