import argparse
from collections import defaultdict
import json
from pathlib import Path
import shutil
import sys

import numpy as np


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
EVIDENCE = WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019"
LEGACY = ROOT.parent / "fair-final-prior-diagnostics-v1"
sys.path[:0] = [str(WORKSPACE / "firstlight_CR"), str(EVIDENCE), str(LEGACY)]

from diagnose_prior import digest, summarize
from delayed_cycle import DelayedCycleAudit
from native_runner.training.fair_final.deck_belief import FrozenDeckPrior, PublicPlay
from native_runner.training.fair_final.refill_belief import native_draw_layout
from native_runner.training.fair_search.contracts import semantic_digest
from verify_migration_evidence import validate_migration_evidence
from verify_refill import validate_refill_trajectory


def verify_native_timing(prior):
    native_report = validate_refill_trajectory()
    paths = [EVIDENCE / relative for relative in native_report["artifacts"]
        if relative.endswith("/project/input-trace.json")]
    if len(paths) != 1:
        raise ValueError("native refill trace missing or ambiguous")
    trace = json.loads(paths[0].read_text())
    native = native_report["shards"][0]["report"]
    events = {owner: [PublicPlay(row["tick"], event["card_id"])
        for row in trace["ticks"] for event in row["public_executions"] if event["owner"] == owner]
        for owner in (0, 1)}
    checked, failures, exact_checked = 0, [], 0
    for owner in (0, 1):
        belief = DelayedCycleAudit(prior, owner=owner, opening_tick=trace["opening_tick"])
        exact = DelayedCycleAudit(prior, owner=owner, opening_tick=trace["opening_tick"])
        opening = native_draw_layout(next(player for player in native["changes"][0]["players"]
            if player["owner"] == owner))
        expected_opening = (*opening.hand, *opening.cycle)
        exact._support = [(entry, initial[keep], current[keep]) for entry, initial, current in exact._support
            if (keep := (current == expected_opening).all(axis=1)).any()]
        if sum(len(current) for _, _, current in exact._support) != 1:
            raise ValueError("native opening does not identify a unique audit reference order")
        for row in native["changes"]:
            plays = tuple(play for play in events[owner] if belief.tick < play.tick <= row["tick"])
            belief.advance(plays, from_tick=belief.tick, to_tick=row["tick"],
                expected_evidence_sha=belief.evidence_sha)
            exact.advance(plays, from_tick=exact.tick, to_tick=row["tick"],
                expected_evidence_sha=exact.evidence_sha)
            player = next(player for player in row["players"] if player["owner"] == owner)
            layout = native_draw_layout(player)
            expected = (*(-1 if card is None else card for card in layout.hand), *layout.cycle)
            if not np.array_equal(exact._support[0][2][0], expected):
                raise ValueError(f"native exact slot/FIFO/timing differs at owner={owner}, tick={row['tick']}")
            exact_checked += 1
            score = belief.score([card for card in layout.hand if card is not None], layout.cycle[0])
            checked += 1
            if not score["truth_in_support"]:
                failures.append(dict(owner=owner, tick=row["tick"], score=score))
    return dict(checked_player_states=checked, exact_slot_fifo_states=exact_checked, failures=failures,
        native_evidence_sha=semantic_digest(native_report),
        exact_reference_inputs="native-opening-order-for-validation-only;never-used-in-prior-predictions",
        scope="single-complete-native-trajectory;exact-draw-timing-and-support-not-full-world-posterior")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=False)
    source = LEGACY / "native-partial-hand-audit-20261022"
    old_plan = json.loads((source / "plan.json").read_text())
    old_report = json.loads((source / "report.json").read_text())
    if (old_report["plan_sha"] != digest(source / "plan.json")
            or old_report["private_audit_sha"] != digest(source / "private-audit.json")):
        raise ValueError("legacy diagnostic data identity differs")
    for relative, expected in old_plan["source_sha256"].items():
        if digest(WORKSPACE / relative) != expected:
            raise ValueError("legacy diagnostic source identity differs")
    provenance = validate_migration_evidence(old_plan["input_attempt"])
    prior_path = EVIDENCE / "public-deck-prior-engineering.json"
    prior = FrozenDeckPrior.from_payload(json.loads(prior_path.read_text()))
    if digest(prior_path) != old_plan["prior_file_sha"] or prior.sha256 != old_plan["prior_sha"]:
        raise ValueError("frozen prior identity differs")
    paths = (Path(__file__), ROOT / "delayed_cycle.py", LEGACY / "diagnose_prior.py",
        WORKSPACE / "firstlight_CR/native_runner/training/fair_search/draw.py",
        WORKSPACE / "firstlight_CR/native_runner/training/fair_final/deck_belief.py")
    plan = dict(schema="fair-final-delayed-prior-audit.v1", prior_sha=prior.sha256,
        input_audit_sha=digest(source / "private-audit.json"),
        input_plan_sha=digest(source / "plan.json"), native_provenance_sha=semantic_digest(provenance),
        prediction_inputs=["frozen-prior", "public-executed-card-prefix", "public-tick"],
        scoring_only_inputs=["target-owner-hand", "target-owner-next"],
        opening_tick=90, training_admissible=False, gates_closed=False,
        source_sha256={str(path.relative_to(WORKSPACE)): digest(path) for path in paths})
    for path in paths:
        archived = args.output / "source" / path.relative_to(WORKSPACE)
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, archived)
    (args.output / "plan.json").write_text(json.dumps(plan, indent=2))
    timing = verify_native_timing(prior)
    (args.output / "native-timing.json").write_text(json.dumps(timing, indent=2))
    if timing["failures"]:
        raise ValueError("public timing approximation fails accepted native trajectory")
    grouped = defaultdict(list)
    for row in json.loads((source / "private-audit.json").read_text()):
        grouped[row["game"], row["target_owner"]].append(row)
    rows = []
    for (game, owner), group in sorted(grouped.items()):
        belief = DelayedCycleAudit(prior, owner=owner, opening_tick=90)
        previous_prefix = ()
        for row in sorted(group, key=lambda row: row["tick"]):
            prefix = tuple(PublicPlay(**play) for play in row["public_prefix"])
            if prefix[:len(previous_prefix)] != previous_prefix:
                raise ValueError("public audit prefix was rewritten")
            belief.advance(prefix[len(previous_prefix):], from_tick=belief.tick, to_tick=row["tick"],
                expected_evidence_sha=belief.evidence_sha)
            prediction = belief.predict()
            before = belief.support_digest()
            score = belief.score(row["truth_hand"], row["truth_next"])
            if belief.support_digest() != before:
                raise ValueError("private scoring labels changed public prediction")
            rows.append(dict(game=game, target_owner=owner, observer=row["observer"], tick=row["tick"],
                row_id=row["row_id"], prediction=prediction, truth_hand=row["truth_hand"],
                truth_next=row["truth_next"], score=score, evidence_sha=belief.evidence_sha))
            previous_prefix = prefix
        print(json.dumps(dict(event="owner_scored", game=game, owner=owner, rows=len(group))), flush=True)
    (args.output / "private-audit.json").write_text(json.dumps(rows, separators=(",", ":")))
    report = dict(schema=plan["schema"], status="completed_diagnostic", plan_sha=digest(args.output / "plan.json"),
        native_timing=timing, overall=summarize(rows),
        by_owner={str(owner): summarize([row for row in rows if row["target_owner"] == owner]) for owner in (0, 1)},
        by_hand_count={str(count): summarize([row for row in rows if len(row["truth_hand"]) == count])
            for count in range(5)},
        by_stage={name: summarize([row for row in rows if lower <= row["tick"] < upper])
            for name, lower, upper in (("0-60s", 0, 1200), ("60-180s", 1200, 3600), ("180s+", 3600, 1000000))},
        truth_support_failures=[row["row_id"] for row in rows if not row["score"]["truth_in_support"]],
        private_audit_sha=digest(args.output / "private-audit.json"), training_admissible=False, gates_closed=False,
        exclusions=["heldout-population-calibration", "hidden-plan-posterior", "full-world-posterior",
            "online-search-changes", "migration-acceptance"])
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(output=str(args.output), overall=report["overall"]["equal_game_mean"],
        support_failures=len(report["truth_support_failures"]))), flush=True)


if __name__ == "__main__":
    main()
