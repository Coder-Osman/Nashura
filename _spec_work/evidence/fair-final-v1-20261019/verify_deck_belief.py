import hashlib
import json
import math
from pathlib import Path
import statistics

from run_deck_belief_validation import (ROOT, SOURCE, WORKSPACE, FrozenDeckPrior,
    PublicDeckRecord, load_histories, plays_of, sha, verify_roots)


def validate_deck_belief(report=None):
    report = report if report is not None else json.loads((ROOT / "deck-belief-report.json").read_text())
    if (report["schema"] != "fair-final-deck-belief-evidence.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or report["source_game_groups"] != 1
            or report["source_seat_records"] != 2 or report["source_unique_decks"] != 1
            or report["source_scope"] != "one-engineering-trajectory-not-population-calibration"
            or report["exclusions"] != ["general-population-calibrated-prior", "exact-policy-weighted-posterior",
                "native-local-reconditioning", "native-search-integration", "training-admission"]):
        raise ValueError("deck belief evidence scope or identity differs")
    histories = load_histories()
    source_path, prior_path = ROOT / "native-history-report.json", ROOT / "public-deck-prior-engineering.json"
    paths = [source_path, prior_path, ROOT / "run_deck_belief_validation.py",
        SOURCE / "native_runner/training/fair_final/deck_belief.py",
        SOURCE / "native_runner/tests/test_fair_final_deck_belief.py"]
    paths.extend(ROOT / relative for relative, _ in histories)
    identities = {str(path.relative_to(WORKSPACE)): sha(path) for path in paths}
    if report["identities"] != identities:
        raise ValueError("deck belief source or input evidence changed")
    frozen = FrozenDeckPrior.from_payload(json.loads(prior_path.read_text()))
    source_sha = sha(source_path)
    public_records = []
    for owner in (0, 1):
        latest = max((history for _, history in histories if history.opening.owner == 1-owner),
            key=lambda history: history.root_tick)
        public_records.append(PublicDeckRecord(source_sha, owner, plays_of(latest)))
    if (frozen != FrozenDeckPrior.from_public_records(public_records, source_sha=source_sha)
            or frozen.sha256 != report["prior_sha"]):
        raise ValueError("deck belief empirical provenance differs")
    if report["records"] != verify_roots(histories, frozen):
        raise ValueError("deck belief support, delta or sampled completion differs")
    benchmark = report["benchmark"]
    if (benchmark["scope"] != "local-card-cycle-filter-and-draw-only" or benchmark["repeats"] != 9
            or len(benchmark["runs"]) != 9
            or benchmark["includes"] != "two incremental constructor costs and twelve sampled roots"
            or benchmark["excludes"] != ["history-prefix-validation", "delta-extraction",
                "native-world-construction", "native-restore-or-reconditioning",
                "model-inference", "MCTS", "complete-games"]):
        raise ValueError("deck belief benchmark scope differs")
    expected_roots = sorted((history.opening.owner, history.root_tick) for _, history in histories)
    for repeat, run in enumerate(benchmark["runs"]):
        order = ["legacy-cold", "incremental"] if repeat % 2 == 0 else ["incremental", "legacy-cold"]
        if run["repeat"] != repeat or run["order"] != order or set(run["timings"]) != set(order):
            raise ValueError("deck belief benchmark run ordering differs")
        for mode, timing in run["timings"].items():
            if [(row["owner"], row["tick"]) for row in timing["roots"]] != expected_roots:
                raise ValueError("deck belief benchmark root coverage differs")
            seconds = [row["seconds"] for row in timing["roots"]]
            initial, total = timing["initialization_seconds"], timing["total_seconds"]
            if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                    for value in [*seconds, total]) or not math.isfinite(initial)
                    or (initial != 0 if mode == "legacy-cold" else initial <= 0)
                    or not math.isclose(total, sum(seconds)+initial, rel_tol=1e-12)):
                raise ValueError("deck belief benchmark timing totals differ")
    medians = {mode: statistics.median(run["timings"][mode]["total_seconds"]
        for run in benchmark["runs"]) for mode in ("legacy-cold", "incremental")}
    if (benchmark["medians"] != medians or not math.isclose(benchmark["median_total_ratio"],
            medians["legacy-cold"]/medians["incremental"], rel_tol=1e-12)):
        raise ValueError("deck belief benchmark summary differs")
    return report


if __name__ == "__main__":
    verified = validate_deck_belief()
    print(json.dumps(dict(status="passed", roots=len(verified["records"]),
        identities=len(verified["identities"]))))
