"""Exact public tower identities for the fingerprint-checked live client.

Chef IDs/names were captured together in 20260920T101254-2b17fb8b.
The pinned static graph defines EXT.ChefTowerKing as BUILDING.KingTower
with ChefTower_CookingAction, and BUILDING.ChefTower as the side tower.
As in BattleEnvV1._tower_troop_id, Chef is encoded on the king; its side
towers use the Princess tower category with their observed HP/position.
"""
from __future__ import annotations

from .snapshot import LiveStateError


_TOWERS = {
    35_000_000: ("KingTower", "king", None),
    35_000_001: ("PrincessTower", "side", 159_000_000),
    1_471_932_914: ("ChefTowerKing", "king", 159_000_004),
    3_191_249_505: ("ChefTower", "side", 159_000_000),
}


def public_tower_identity(entity: dict) -> tuple[str, int | None] | None:
    identity = _TOWERS.get(entity.get("native_data_id"))
    if identity is None:
        return None
    name, kind, troop = identity
    # Never recognize a tower from its name or position alone. Old captures
    # without names retain their exact numeric identity; conflicting names fail.
    # Live captures show the same crown tower identity with kind 12 or 13
    # over its lifetime; this field is not a stable building discriminator.
    if entity.get("kind") not in (12, 13) or entity.get("native_data_name", name) != name:
        raise LiveStateError("public tower identity mismatch")
    if kind == "side":
        kind = "princess_left" if entity["x"] < 9000 else "princess_right"
    return kind, troop
