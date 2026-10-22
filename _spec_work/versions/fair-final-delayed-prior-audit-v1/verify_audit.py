import argparse
from collections import defaultdict
import json
from pathlib import Path

from run_audit import (
    DelayedCycleAudit, EVIDENCE, FrozenDeckPrior, LEGACY, PublicPlay, WORKSPACE,
    digest, semantic_digest, summarize, validate_migration_evidence, verify_native_timing,
)


def normalized(value):
    return json.loads(json.dumps(value))


def validate(attempt):
    attempt = Path(attempt)
    plan = json.loads((attempt / "plan.json").read_text())
    report = json.loads((attempt / "report.json").read_text())
    if (plan["schema"] != "fair-final-delayed-prior-audit.v1" or report["schema"] != plan["schema"]
            or report["status"] != "completed_diagnostic" or report["plan_sha"] != digest(attempt / "plan.json")
            or report["private_audit_sha"] != digest(attempt / "private-audit.json")
            or any(record[key] is not False for record in (plan, report)
                for key in ("training_admissible", "gates_closed"))):
        raise ValueError("delayed diagnostic identity, scope or integrity differs")
    for relative, expected in plan["source_sha256"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("diagnostic source path escapes workspace")
        if (digest(WORKSPACE / relative) != expected
                or digest(attempt / "source" / relative) != expected):
            raise ValueError("current or archived diagnostic source differs")
    source = LEGACY / "native-partial-hand-audit-20261022"
    legacy_plan = json.loads((source / "plan.json").read_text())
    if (plan["input_audit_sha"] != digest(source / "private-audit.json")
            or plan["input_plan_sha"] != digest(source / "plan.json")
            or plan["native_provenance_sha"] != semantic_digest(
                validate_migration_evidence(legacy_plan["input_attempt"]))):
        raise ValueError("accepted native input identity differs")
    prior = FrozenDeckPrior.from_payload(json.loads((EVIDENCE / "public-deck-prior-engineering.json").read_text()))
    if plan["prior_sha"] != prior.sha256:
        raise ValueError("frozen prior differs")
    timing = verify_native_timing(prior)
    if (timing["failures"] or normalized(timing) != report["native_timing"]
            or normalized(timing) != json.loads((attempt / "native-timing.json").read_text())):
        raise ValueError("native exact timing evidence differs")
    originals = json.loads((source / "private-audit.json").read_text())
    rows = json.loads((attempt / "private-audit.json").read_text())
    expected_rows = {row["row_id"]: row for row in originals}
    if (len(expected_rows) != len(originals) or len(rows) != len(originals)
            or len({row["row_id"] for row in rows}) != len(rows)
            or {row["row_id"] for row in rows} != set(expected_rows)):
        raise ValueError("diagnostic rows missing, duplicated or extra")
    grouped = defaultdict(list)
    for row in rows:
        original = expected_rows[row["row_id"]]
        if any(row[key] != original[key] for key in
                ("game", "target_owner", "observer", "tick", "truth_hand", "truth_next")):
            raise ValueError("diagnostic row identity or held-out label differs")
        grouped[row["game"], row["target_owner"]].append(row)
    for (_, owner), group in sorted(grouped.items()):
        belief = DelayedCycleAudit(prior, owner=owner, opening_tick=plan["opening_tick"])
        for row in sorted(group, key=lambda row: row["tick"]):
            original = expected_rows[row["row_id"]]
            plays = tuple(PublicPlay(**play) for play in original["public_prefix"]
                if belief.tick < play["tick"] <= row["tick"])
            belief.advance(plays, from_tick=belief.tick, to_tick=row["tick"],
                expected_evidence_sha=belief.evidence_sha)
            if (normalized(belief.predict()) != row["prediction"]
                    or normalized(belief.score(row["truth_hand"], row["truth_next"])) != row["score"]
                    or belief.evidence_sha != row["evidence_sha"]):
                raise ValueError("public-only replay or score differs")
    summary_rows = [dict(row, prediction=dict(row["prediction"],
        next_card={int(card): probability for card, probability in row["prediction"]["next_card"].items()}))
        for row in rows]
    summaries = dict(overall=summarize(summary_rows),
        by_owner={str(owner): summarize([row for row in summary_rows if row["target_owner"] == owner]) for owner in (0, 1)},
        by_hand_count={str(count): summarize([row for row in summary_rows if len(row["truth_hand"]) == count])
            for count in range(5)},
        by_stage={name: summarize([row for row in summary_rows if lower <= row["tick"] < upper])
            for name, lower, upper in (("0-60s", 0, 1200), ("60-180s", 1200, 3600), ("180s+", 3600, 1000000))})
    if any(normalized(value) != report[key] for key, value in summaries.items()):
        raise ValueError("diagnostic summary differs from equal-game calculation")
    failures = [row["row_id"] for row in rows if not row["score"]["truth_in_support"]]
    if report["truth_support_failures"] != failures:
        raise ValueError("support failure list differs")
    return dict(status="passed_limited_diagnostic", rows=len(rows), games=len(grouped) // 2,
        support_failures=len(failures), exact_native_states=timing["exact_slot_fifo_states"],
        report_sha=digest(attempt / "report.json"), verifier_sha=digest(__file__),
        training_admissible=False, gates_closed=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    args = parser.parse_args()
    result = validate(args.attempt)
    (args.attempt / "verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
