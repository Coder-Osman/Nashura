"""Opt-in policy constraints, separate from native placement legality.

Lightning needs a public living enemy near its chosen cell; The Log needs a
ground target along its forward sweep. The extra margin allows anticipation.
These are proximity priors, not hit guarantees.
All geometry uses native world coordinates before the actor transform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import math

from ..arena import CELL_UNITS, cell_to_world
from ..card_specs import build_card_catalog
from ..card_logic import build_static_card_logic_catalog

LIGHTNING_CARD_ID = 28_000_007
LOG_CARD_ID = 28_000_011
DEFAULT_LIGHTNING_MARGIN_TILES = 0.5
DEFAULT_LOG_MARGIN_TILES = 0.5


@lru_cache(maxsize=1)
def _lightning_radius_tiles() -> float:
    spec = build_card_catalog().by_id[LIGHTNING_CARD_ID]
    area = spec.attributes["resolved_area_effect"]
    radius = float(area["Radius"]) / CELL_UNITS
    if (not math.isfinite(radius) or radius <= 0 or spec.radius_tiles != radius
            or area.get("OnlyEnemies") is not True
            or area.get("HitsAir") is not True or area.get("HitsGround") is not True):
        raise ValueError("Lightning target constraint requires the verified enemy/air/ground radius")
    return radius


@dataclass(frozen=True, slots=True)
class LightningTargetConstraintV1:
    margin_tiles: float = DEFAULT_LIGHTNING_MARGIN_TILES
    spell_radius_tiles: float = field(init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.margin_tiles) or self.margin_tiles < 0:
            raise ValueError("Lightning target margin must be finite and non-negative")
        object.__setattr__(self, "spell_radius_tiles", _lightning_radius_tiles())

    @property
    def radius_tiles(self) -> float:
        return self.spell_radius_tiles + self.margin_tiles

    def to_dict(self) -> dict[str, object]:
        return {
            "version": "lightning-target-constraint.v1",
            "card_id": LIGHTNING_CARD_ID,
            "spell_radius_tiles": self.spell_radius_tiles,
            "margin_tiles": self.margin_tiles,
            "target_radius_tiles": self.radius_tiles,
            "targets": "public_living_enemies_including_buildings_and_towers",
            "geometry": "native_cell_center_to_target_center_inclusive",
            "checks": "decision_and_submission",
            "on_missing_at_submission": "cancel_keep_behavior_row_and_wake",
        }


@lru_cache(maxsize=1)
def _log_geometry_tiles() -> tuple[float, float, float]:
    spec = build_card_catalog().by_id[LOG_CARD_ID]
    falling = spec.attributes["resolved_projectile"]
    rolling = build_static_card_logic_catalog().nodes[
        f"PROJECTILE.{falling['SpawnProjectile']}"
    ]["effective_record"]
    dimensions = tuple(float(rolling[key]) / CELL_UNITS
                       for key in ("ProjectileRange", "ProjectileRadius", "ProjectileRadiusY"))
    if (falling.get("SpawnProjectile") != "LogProjectileRolling"
            or rolling.get("OnlyEnemies") is not True or rolling.get("AoeToGround") is not True
            or rolling.get("AoeToAir", False) or any(not math.isfinite(v) or v <= 0 for v in dimensions)):
        raise ValueError("Log target constraint requires verified enemy/ground forward-sweep geometry")
    return dimensions


@dataclass(frozen=True, slots=True)
class LogTargetConstraintV1:
    margin_tiles: float = DEFAULT_LOG_MARGIN_TILES
    travel_tiles: float = field(init=False)
    half_width_tiles: float = field(init=False)
    half_depth_tiles: float = field(init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.margin_tiles) or self.margin_tiles < 0:
            raise ValueError("Log target margin must be finite and non-negative")
        for name, value in zip(("travel_tiles", "half_width_tiles", "half_depth_tiles"), _log_geometry_tiles()):
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": "log-target-constraint.v1",
            "card_id": LOG_CARD_ID,
            "travel_tiles": self.travel_tiles,
            "half_width_tiles": self.half_width_tiles,
            "half_depth_tiles": self.half_depth_tiles,
            "margin_tiles": self.margin_tiles,
            "targets": "public_living_ground_enemies_including_buildings_and_towers",
            "target_identity": "runtime_global_id_or_current_form_no_parent_card_fallback",
            "geometry": "forward_swept_rectangle_to_target_center_inclusive",
            "direction": "native_positive_y_for_owner_0_negative_y_for_owner_1",
            "checks": "decision_and_submission",
            "on_missing_at_submission": "cancel_keep_behavior_row_and_wake",
        }


@lru_cache(maxsize=1)
def _target_archetypes():
    from .v4.catalog import EntityArchetypeCatalogV1, normal_mode_card_specs

    return EntityArchetypeCatalogV1.from_card_specs(normal_mode_card_specs(build_card_catalog().by_id))


def _known_ground_entity(target) -> bool:
    """Use the live child's identity, not the card that spawned it (e.g. Bats)."""
    catalog = _target_archetypes()
    global_id = getattr(target, "native_data_global_id", None)
    if isinstance(global_id, int) and not isinstance(global_id, bool):
        vocab_id = catalog.runtime_global_vocab_id(global_id)
    else:
        evolution = getattr(target, "evolution_state", None)
        vocab_id = catalog.form_vocab_id(getattr(evolution, "current_form_id", "") or "")
    metadata = catalog.metadata_for_vocab_id(vocab_id)
    return (metadata.child_kind_known and metadata.child_kind in {"character", "building"}
            and metadata.is_airborne_known and not metadata.is_airborne)


def _target_positions(observation, owner: int, *, ground_only: bool = False) -> tuple[tuple[float, float], ...]:
    """Use only the actor observation; never inspect privileged native objects.

    Do not require troop kind: units spawned by spells can inherit a spell card
    identity. Positive HP excludes area effects; projectiles are explicitly out.
    Single-target invisibility is not area-damage immunity, so it is not a veto.
    """
    if owner not in (0, 1):
        raise ValueError("owner must be 0 or 1")
    positions = []
    for target in (*observation.entities, *observation.towers):
        hp = target.hitpoints
        if target.owner != 1 - owner or hp is None or not math.isfinite(hp) or hp <= 0:
            continue
        if getattr(target, "entity_kind", None) in {"projectile", "effect"}:
            continue
        if getattr(target, "projectile_state", None) is not None or getattr(target, "visible", None) is False:
            continue
        visibility = target.visibility_state
        if visibility is not None and (
            visibility.public_by_owner.get(str(owner)) is False or visibility.area_damage_eligible is False
        ):
            continue
        if ground_only and getattr(target, "tower_kind", None) is None and not _known_ground_entity(target):
            continue
        x, y = target.position
        if math.isfinite(x) and math.isfinite(y):
            positions.append((float(x), float(y)))
    return tuple(positions)


def _near_target(cell, targets, radius_units: float) -> bool:
    x, y = cell_to_world(cell)
    return any((x - tx) ** 2 + (y - ty) ** 2 <= radius_units ** 2 for tx, ty in targets)


def _in_log_sweep(cell, targets, owner: int, constraint: LogTargetConstraintV1) -> bool:
    x, y = cell_to_world(cell)
    direction = 1 if owner == 0 else -1
    half_width = (constraint.half_width_tiles + constraint.margin_tiles) * CELL_UNITS
    half_depth = (constraint.half_depth_tiles + constraint.margin_tiles) * CELL_UNITS
    travel = constraint.travel_tiles * CELL_UNITS
    return any(abs(tx - x) <= half_width and -half_depth <= direction * (ty - y) <= travel + half_depth
               for tx, ty in targets)


def log_target_allowed(observation, owner: int, card_id: int, native_cell,
                       constraint: LogTargetConstraintV1 | None = None) -> bool:
    if constraint is None or int(card_id) != LOG_CARD_ID:
        return True
    return _in_log_sweep(native_cell, _target_positions(observation, owner, ground_only=True), owner, constraint)


def lightning_target_allowed(observation, owner: int, card_id: int, native_cell,
                             constraint: LightningTargetConstraintV1 | None = None) -> bool:
    """Recheck a scheduled plan against the latest actor observation."""
    if constraint is None or int(card_id) != LIGHTNING_CARD_ID:
        return True
    return _near_target(native_cell, _target_positions(observation, owner), constraint.radius_tiles * CELL_UNITS)


def constrain_lightning_placement(rows, observation, owner: int, card_id: int,
                                  constraint: LightningTargetConstraintV1 | None = None):
    """Intersect native placement rows without mutating engine-owned masks."""
    if constraint is None or int(card_id) != LIGHTNING_CARD_ID:
        return rows
    targets = _target_positions(observation, owner)
    radius_units = constraint.radius_tiles * CELL_UNITS
    return [[bool(value) and _near_target((x, y), targets, radius_units)
             for x, value in enumerate(row)] for y, row in enumerate(rows)]


def constrain_spell_placement(rows, observation, owner: int, card_id: int, *,
                              lightning_constraint: LightningTargetConstraintV1 | None = None,
                              log_constraint: LogTargetConstraintV1 | None = None):
    if log_constraint is not None and int(card_id) == LOG_CARD_ID:
        targets = _target_positions(observation, owner, ground_only=True)
        return [[bool(value) and _in_log_sweep((x, y), targets, owner, log_constraint)
                 for x, value in enumerate(row)] for y, row in enumerate(rows)]
    return constrain_lightning_placement(rows, observation, owner, card_id, lightning_constraint)


def spell_target_allowed(observation, owner: int, card_id: int, native_cell, *,
                         lightning_constraint: LightningTargetConstraintV1 | None = None,
                         log_constraint: LogTargetConstraintV1 | None = None) -> bool:
    return (lightning_target_allowed(observation, owner, card_id, native_cell, lightning_constraint)
            and log_target_allowed(observation, owner, card_id, native_cell, log_constraint))


def add_lightning_target_arguments(parser) -> None:
    parser.add_argument(
        "--lightning-target-constraint", action="store_true",
        help="require a public living enemy near Lightning at decision and submission (default: off)",
    )
    parser.add_argument(
        "--lightning-target-margin-tiles", type=float, default=DEFAULT_LIGHTNING_MARGIN_TILES,
        help="extra distance beyond Lightning's resource radius; default 0.5 tiles",
    )


def lightning_constraint_from_args(args) -> LightningTargetConstraintV1 | None:
    if not args.lightning_target_constraint:
        if args.lightning_target_margin_tiles != DEFAULT_LIGHTNING_MARGIN_TILES:
            raise ValueError("--lightning-target-margin-tiles requires --lightning-target-constraint")
        return None
    return LightningTargetConstraintV1(margin_tiles=args.lightning_target_margin_tiles)


def add_log_target_arguments(parser) -> None:
    parser.add_argument(
        "--log-target-constraint", action="store_true",
        help="require a public living ground enemy along The Log's sweep at decision and submission (default: off)",
    )
    parser.add_argument(
        "--log-target-margin-tiles", type=float, default=DEFAULT_LOG_MARGIN_TILES,
        help="extra distance around The Log's swept rectangle; default 0.5 tiles",
    )


def log_constraint_from_args(args) -> LogTargetConstraintV1 | None:
    if not args.log_target_constraint:
        if args.log_target_margin_tiles != DEFAULT_LOG_MARGIN_TILES:
            raise ValueError("--log-target-margin-tiles requires --log-target-constraint")
        return None
    return LogTargetConstraintV1(margin_tiles=args.log_target_margin_tiles)


def action_constraints_contract(constraint: LightningTargetConstraintV1 | None,
                                log_constraint: LogTargetConstraintV1 | None = None) -> dict[str, object]:
    from .event_delay_constraints import delay_constraints_contract

    result = delay_constraints_contract()
    if constraint is not None:
        result["lightning"] = constraint.to_dict()
    if log_constraint is not None:
        result["log"] = log_constraint.to_dict()
    return result
