"""Project verified live component fields through the training resolvers."""
from __future__ import annotations

from ..phase_runtime import (AttackRuntimeRaw, DeploymentRuntimeRaw, MovementRuntimeRaw, PhaseHookEvent,
                             resolve_attack_runtime, resolve_deployment_runtime, resolve_movement_runtime)
from ..contracts import (SemanticProvenanceV1, ENTITY_RUNTIME_SEMANTIC_FIELDS,
                          TOWER_RUNTIME_SEMANTIC_FIELDS, VisibilityStateV1, VISIBILITY_STATE_FIELDS)
from .snapshot import LiveStateError


# Exact identities in the pinned card_logic bundle. Both effective records
# carry NO_DAMAGE and UNTARGETABLE. Keep these action-chain actors in the
# model; they are not damage targets. Monster/Skeleton forms are separate.
_TOMBSTONE_HELPERS = {
    2573438603: "TombstoneHero_visual_dummy",
    3587309677: "TombstoneHero_broken_visual_dummy",
}


def _helper_runtime(row, entity):
    name = _TOMBSTONE_HELPERS.get(entity.get("native_data_id"))
    if name is None:
        return {}
    if entity.get("native_data_name") != name:
        raise LiveStateError("hero helper native identity mismatch")
    known = {"targetable_by_owner", "area_damage_eligible"}
    source = f"pinned card_logic/{name}: GameTagsToSet=NO_DAMAGE,UNTARGETABLE"
    return {"visibility_state": VisibilityStateV1(
        targetable_by_owner={"0": False, "1": False}, area_damage_eligible=False,
        provenance=SemanticProvenanceV1(
            field_evidence={field: "static_declared" if field in known else "unknown"
                            for field in VISIBILITY_STATE_FIELDS},
            source_fields={field: (source,) for field in known}, observed_tick=row["tick"],
            notes=("Exact public helper identity; static tags, not measured runtime visibility",)),
    )}


def runtime_provenance(row, values, *, tower=False):
    universe = TOWER_RUNTIME_SEMANTIC_FIELDS if tower else ENTITY_RUNTIME_SEMANTIC_FIELDS
    present = set(values).intersection(universe)
    return SemanticProvenanceV1(
        field_evidence={name: ("static_declared" if name == "visibility_state" else "native_derived")
                        if name in present else "unknown" for name in universe},
        source_fields={name: (("exact helper identity / pinned card_logic GameTagsToSet",)
                             if name == "visibility_state" else
                             ("live exact-build component fields / training phase resolver",))
                       for name in present},
        observed_tick=row["tick"], notes=("Visible exact-build phase facts; uncaptured fields stay unknown",))


def entity_runtime(row, entity):
    result = _helper_runtime(row, entity)
    phase = entity.get("phase")
    if not phase:
        return result
    key = (entity["owner"], -2, entity["id"])
    target = phase.get("target_id")
    visible = {e["id"] for e in row["entities"]}
    target_known = bool(phase.get("target_validated") and (target is None or target in visible))
    target = target if target_known else None
    hooks = phase.get("hooks_attested") is True
    events = tuple(PhaseHookEvent(entity_key=key, **event) for event in phase.get("events", ())) if hooks else ()
    attack_step = phase.get("attack_step", {}).get("value")
    attack = resolve_attack_runtime(AttackRuntimeRaw(
        entity_key=key, tick=row["tick"], target_validated=target_known, target_entity=target,
        attack_sequence_stage=phase["attack_sequence_stage"] if phase["attack_sequence_stage"] >= 0 else None,
        attack_timeline_ms=phase["attack_timeline_ms"], load_remaining_ms=phase["load_remaining_ms"],
        hit_speed_ms=phase["hit_speed_ms"], attack_dash_time_ms=phase["attack_dash_time_ms"],
        attack_step_native_ms=attack_step, hook_set_attested=hooks, events=events,
    )).state
    remaining, previous = phase["deploy_remaining_ms"], phase["deploy_previous_ms"]
    deployment = resolve_deployment_runtime(DeploymentRuntimeRaw(
        tick=row["tick"], component_validated=True, remaining_native_ms=remaining,
        previous_remaining_native_ms=previous, configured_deploy_time_ms=phase["configured_deploy_time_ms"],
        uses_effect_scaled_step=True if "deploy_step" in phase else (False if remaining == 0 or previous-remaining == 50 else None),
        observed_step_native_ms=phase.get("deploy_step", {}).get("value"),
    ))
    movement = resolve_movement_runtime(MovementRuntimeRaw(
        entity_key=key, tick=row["tick"], component_validated=phase["movement_validated"],
        hook_set_attested=hooks, movement_delta=phase.get("movement_delta", {}).get("value"),
        effective_speed=phase.get("effective_speed", {}).get("value"),
        speed_input=phase.get("movement_step", {}).get("input"),
        speed_after_effects=phase.get("movement_step", {}).get("value"),
        classic_charge_progress=phase.get("classic_charge_progress"),
        classic_charge_speed_multiplier=phase.get("charge_speed_multiplier"),
    ))
    result.update(attack_state=attack, deployment_runtime=deployment.to_mapping(),
                  movement_runtime=movement.to_mapping())
    if target_known:
        result["visible_target"] = target
    return result
