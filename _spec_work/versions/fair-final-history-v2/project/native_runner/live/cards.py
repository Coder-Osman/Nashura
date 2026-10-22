"""Project the actor's verified native selection forms into model inputs."""
from __future__ import annotations

from ..contracts import (EVOLUTION_RUNTIME_STATE_FIELDS, EvolutionRuntimeStateV1,
                         SemanticProvenanceV1)
from .snapshot import LiveStateError


def placement_form(card, specs):
    spec = specs.get(card["card_id"])
    code = card.get("form_code")
    if spec is None:
        raise LiveStateError(f"unknown actor card: {card['card_id']}")
    if type(code) is int:
        if code == 0:
            return "base"
        if code == 1 and spec.evolution is not None:
            return "evolution"
    raise LiveStateError(f"unsupported live card form: card={card['card_id']} ({spec.name}) "
                         f"deck_slot={card['deck_slot']} form_code={code}")


def ready_evolutions(row, specs):
    states = []
    for card in row["own"]["deck"]:
        if placement_form(card, specs) != "evolution":
            continue
        evolution = specs[card["card_id"]].evolution
        native = {"deck_slot", "phase", "cycle_remaining", "ready"}
        static = {"base_form_id", "next_form_id", "cycle_required"}
        states.append(EvolutionRuntimeStateV1(
            card_id=card["card_id"], deck_slot=card["deck_slot"], phase="ready",
            base_form_id=evolution.base_form_id, next_form_id=evolution.evolution_form_id,
            cycle_required=evolution.cycle_required, cycle_remaining=0, ready=True,
            provenance=SemanticProvenanceV1(
                field_evidence={key: "native_derived" if key in native else
                                "static_declared" if key in static else "unknown"
                                for key in EVOLUTION_RUNTIME_STATE_FIELDS},
                source_fields={**{key: ("own.deck native selection form_code=1",) for key in native},
                               **{key: ("pinned CardSpec.evolution",) for key in static}},
                observed_tick=row["tick"], notes=(
                    "Current selection proves readiness even while the card is out of hand",
                    "No equipped-slot, non-ready cycle progress or deployed-entity state is inferred",)),
        ))
    return tuple(states)
