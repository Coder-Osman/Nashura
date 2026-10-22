from copy import deepcopy
import hashlib
import json

from verify_deck_belief import ROOT, validate_deck_belief


CASES = ("missing_root", "duplicate_root", "changed_completion", "wrong_support",
    "wrong_delta", "wrong_prior", "changed_source", "missing_identity", "training_claim",
    "population_claim", "wrong_ratio", "missing_timing", "wrong_ab_order", "missing_exclusion")


def mutate(report, case):
    if case == "missing_root":
        report["records"].pop()
    elif case == "duplicate_root":
        report["records"][1] = report["records"][0]
    elif case == "changed_completion":
        report["records"][0]["sample_commitments"][0] = "0" * 64
    elif case == "wrong_support":
        report["records"][0]["compatible_orders"] += 1
    elif case == "wrong_delta":
        report["records"][1]["order_checks"] += 1
    elif case == "wrong_prior":
        report["prior_sha"] = "0" * 64
    elif case == "changed_source":
        report["identities"][next(iter(report["identities"]))] = "0" * 64
    elif case == "missing_identity":
        report["identities"].pop(next(iter(report["identities"])))
    elif case == "training_claim":
        report["training_admissible"] = True
    elif case == "population_claim":
        report["source_scope"] = "population-calibrated"
    elif case == "wrong_ratio":
        report["benchmark"]["median_total_ratio"] *= 2
    elif case == "missing_timing":
        report["benchmark"]["runs"][0]["timings"]["incremental"]["roots"].pop()
    elif case == "wrong_ab_order":
        report["benchmark"]["runs"][1]["order"].reverse()
    elif case == "missing_exclusion":
        report["exclusions"].pop()
    else:
        raise ValueError("unknown negative case")


def main():
    report = validate_deck_belief()
    checks = []
    for case in CASES:
        altered = deepcopy(report)
        mutate(altered, case)
        try:
            validate_deck_belief(altered)
        except ValueError:
            checks.append(dict(case=case, rejected=True))
        else:
            raise AssertionError("accepted tampered deck belief evidence: " + case)
    result = dict(status="passed", positive_roots=len(report["records"]), negative_cases=checks,
        report_sha256=hashlib.sha256((ROOT / "deck-belief-report.json").read_bytes()).hexdigest())
    (ROOT / "deck-belief-validator-tests.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(dict(status="passed", negative_cases=len(checks))))


if __name__ == "__main__":
    main()
