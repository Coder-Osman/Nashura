import argparse
from collections import defaultdict
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
EVIDENCE = WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019"
sys.path[:0] = [str(WORKSPACE / "firstlight_CR"), str(EVIDENCE)]

import numpy as np
import torch

from native_runner.training.fair_final.deck_belief import FrozenDeckPrior, PublicCycleBelief, PublicPlay
from native_runner.training.fair_migration_data import MigrationStore, information_from_row
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.v4.factory import production_semantic_bundle
from verify_migration_evidence import validate_migration_evidence


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def predict(cursor):
    cards = sorted({card for entry, _, _ in cursor._support for card in entry.cards})
    total = sum(entry.count * len(current) for entry, _, current in cursor._support)
    hand, next_card = {}, {}
    for card in cards:
        hand[card] = sum(entry.count * int((current[:, :4] == card).any(1).sum())
            for entry, _, current in cursor._support) / total
        next_card[card] = sum(entry.count * int((current[:, 4] == card).sum())
            for entry, _, current in cursor._support) / total
    if not math.isclose(sum(hand.values()), 4.) or not math.isclose(sum(next_card.values()), 1.):
        raise ValueError("cycle marginals have inconsistent mass")
    return dict(cycle_front_four_inclusion=hand, next_card=next_card,
        orders=sum(len(current) for _, _, current in cursor._support))


def truth_from_row(row, catalog):
    own = information_from_row(row).frame.own_cards
    mapping = {catalog.vocab_id(card): card for card in HOG_DECK}
    hand, upcoming = [], []
    for index in range(own.mask.shape[1]):
        if not bool(own.mask[0, index]):
            continue
        card = mapping[int(own.card_vocab_id[0, index])]
        flags = own.runtime_features[0, index, :2].tolist()
        if any(flag not in (0., 1.) for flag in flags):
            raise ValueError("audited hand/next flags must be binary")
        if flags[0]:
            hand.append(card)
        if flags[1]:
            upcoming.append(card)
    if (not 0 <= len(hand) <= 4 or len(set(hand)) != len(hand)
            or len(upcoming) != 1 or upcoming[0] in hand):
        raise ValueError("audited own hand/next truth differs from fixed-deck rules")
    return sorted(hand), upcoming[0]


def score(prediction, hand, upcoming, cursor):
    cards = sorted(prediction["cycle_front_four_inclusion"])
    if set(hand) - set(cards) or upcoming not in cards:
        raise ValueError("true cards fall outside prior support")
    total = sum(entry.count * len(current) for entry, _, current in cursor._support)
    compatible = 0
    for entry, _, current in cursor._support:
        contains_hand = np.isin(current[:, :4], hand).sum(1) == len(hand)
        compatible += entry.count * int((contains_hand & (current[:, 4] == upcoming)).sum())
    probability = prediction["next_card"][upcoming]
    return dict(next_brier=sum((prediction["next_card"][card] - (card == upcoming))**2 for card in cards),
        next_nll=None if probability == 0 else -math.log(probability),
        zero_next_probability=probability == 0, truth_in_support=compatible > 0,
        joint_compatible_probability_mass=compatible / total)


def summarize(rows):
    games = defaultdict(list)
    for row in rows:
        games[row["game"]].append(row)
    metrics = ("next_brier", "truth_in_support", "zero_next_probability", "joint_compatible_probability_mass")
    means = {game: {key: math.fsum(float(row["score"][key]) for row in records) / len(records)
        for key in metrics} for game, records in games.items()}
    result = dict(games=len(games), rows=len(rows), game_means=means)
    if not rows:
        return dict(result, equal_game_mean=None, next_reliability_frame_weighted_descriptive=[])
    result["equal_game_mean"] = {key: math.fsum(game[key] for game in means.values()) / len(means) for key in metrics}
    if not any(row["score"]["zero_next_probability"] for row in rows):
        result["equal_game_next_nll"] = math.fsum(math.fsum(row["score"]["next_nll"] for row in records)
            / len(records) for records in games.values()) / len(games)
    bins = []
    for lower in range(10):
        samples = [(probability, card == row["truth_next"]) for row in rows
            for card, probability in row["prediction"]["next_card"].items()
            if min(9, int(probability * 10)) == lower]
        if samples:
            bins.append(dict(lower=lower / 10, count=len(samples),
                predicted=math.fsum(item[0] for item in samples) / len(samples),
                observed=math.fsum(item[1] for item in samples) / len(samples)))
    result["next_reliability_frame_weighted_descriptive"] = bins
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("diagnostic attempt must be new")
    args.output.mkdir(parents=True)
    torch.set_num_threads(1)
    prior_path = EVIDENCE / "public-deck-prior-engineering.json"
    prior = FrozenDeckPrior.from_payload(json.loads(prior_path.read_text()))
    source_report = EVIDENCE / "native-history-report.json"
    if prior.source_sha != digest(source_report):
        raise ValueError("frozen empirical prior source differs")
    if len(prior.entries) != 1 or prior.entries[0].cards != tuple(sorted(HOG_DECK)):
        raise ValueError("this diagnostic is limited to the frozen single-deck experiment")
    native_attempt = "native-migration-1792548905478386000"
    plan = dict(schema="fair-final-offline-cycle-audit.v2", prior_sha=prior.sha256,
        prior_file_sha=digest(prior_path), input_attempt=native_attempt,
        input_finalization_sha=digest(EVIDENCE / native_attempt / "finalization.json"),
        prediction_inputs=["frozen-prior", "past-public-executed-cards", "target-owner-decision-tick"],
        score_only_inputs=["target-owner-own-frame-hand", "target-owner-own-frame-next-card"],
        stage_ticks=[1200, 3600], all_decision_rows=True, fit_performed=False,
        metrics=["next-brier", "next-nll", "truth-support", "joint-compatible-mass", "next-reliability"],
        compatibility_semantics="observed-0-to-4-hand-cards-subset-of-cycle-front-four-and-exact-next-card",
        approximation="instantaneous-refill-cycle-support-not-native-hand-posterior",
        summary_weighting="equal-game", training_admissible=False,
        source_sha256={str(path.relative_to(WORKSPACE)): digest(path) for path in
            (Path(__file__), WORKSPACE / "firstlight_CR/native_runner/training/fair_final/deck_belief.py",
             WORKSPACE / "firstlight_CR/native_runner/training/v4/tensorizer.py")})
    (args.output / "plan.json").write_text(json.dumps(plan, indent=2))
    provenance = validate_migration_evidence(native_attempt)
    directory = EVIDENCE / native_attempt / "evidence"
    manifest = json.loads((directory / "manifest.json").read_text())
    store = MigrationStore(directory / "data", manifest=manifest)
    catalog = production_semantic_bundle().card_catalog
    rows, inputs = [], []
    for match in sorted(manifest["matches"]):
        episode, receipt = store.load(match)
        inputs.append(receipt)
        decisions = {owner: [row for row in episode["rows"] if row["owner"] == owner] for owner in (0, 1)}
        for observer in (0, 1):
            target = 1 - observer
            observer_rows, target_rows = decisions[observer], decisions[target]
            observer_by_tick = {row["tick"]: row for row in observer_rows}
            target_by_tick = {row["tick"]: row for row in target_rows}
            matched_ticks = sorted(observer_by_tick.keys() & target_by_tick.keys())
            if not matched_ticks:
                raise ValueError("native dataset has no paired same-tick FAIR observations")
            public_plays = tuple(sorted((PublicPlay(row["outcome"]["tick"], row["behavior_action"]["card_id"])
                for row in target_rows if row["outcome"]["reason"] == "executed"
                and row["behavior_action"]["card_id"] is not None), key=lambda play: play.tick))
            cursor = PublicCycleBelief(prior, owner=target, opening_tick=observer_rows[0]["tick"])
            for tick in matched_ticks:
                row, truth_row = observer_by_tick[tick], target_by_tick[tick]
                if information_from_row(row).owner != observer or information_from_row(truth_row).owner != target:
                    raise ValueError("paired FAIR owner identity differs")
                prefix = tuple(play for play in public_plays if play.tick <= tick)
                delta = tuple(play for play in prefix if cursor.tick < play.tick)
                cursor.advance(delta, from_tick=cursor.tick, to_tick=tick, expected_evidence_sha=cursor.evidence_sha)
                prediction = predict(cursor)
                hand, upcoming = truth_from_row(truth_row, catalog)
                result = score(prediction, hand, upcoming, cursor)
                rows.append(dict(game=match, target_owner=target, observer=observer, tick=tick, row_id=row["row_id"],
                    public_prefix=[asdict(play) for play in prefix], evidence_sha=cursor.evidence_sha,
                    revealed_cards=len({play.card_id for play in prefix}),
                    prediction=prediction, truth_hand=hand, truth_next=upcoming, score=result))
        print(json.dumps(dict(event="game_scored", game=match, rows=len(episode["rows"]),
            paired_same_tick=sum(row["game"] == match for row in rows))), flush=True)
    report = dict(schema=plan["schema"], status="completed_diagnostic", plan_sha=digest(args.output / "plan.json"),
        native_provenance_sha=semantic_digest(provenance), input_receipts_sha=semantic_digest(inputs),
        overall=summarize(rows), by_owner={str(owner): summarize([row for row in rows if row["target_owner"] == owner])
            for owner in (0, 1)}, by_stage={}, by_reveal={}, training_admissible=False, gates_closed=False,
        empirical_vs_uniform_card_deck_prior="identical-single-supported-deck-frequency-cancels",
        exclusions=["population-calibration", "policy-likelihood-posterior", "native-hidden-plan",
            "root-controller-or-search-improvement", "observer-ready-boundary-coverage", "heldout-confirmatory-test"],
        truth_support_failures=[row["row_id"] for row in rows if not row["score"]["truth_in_support"]])
    for name, lower, upper in (("0-60s", 0, 1200), ("60-180s", 1200, 3600), ("180s+", 3600, 1000000)):
        selected = [row for row in rows if lower <= row["tick"] < upper]
        if selected:
            report["by_stage"][name] = summarize(selected)
    report["public_eight_card_revealed"] = summarize([row for row in rows if row["revealed_cards"] == 8])
    report["public_partial_deck"] = summarize([row for row in rows if row["revealed_cards"] < 8])
    for name, revealed in (("unrevealed", False), ("revealed", True)):
        selected = [row for row in rows if (row["revealed_cards"] == 8) == revealed]
        if selected:
            report["by_reveal"][name] = summarize(selected)
    (args.output / "private-audit.json").write_text(json.dumps(rows, separators=(",", ":")))
    report["private_audit_sha"] = digest(args.output / "private-audit.json")
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(output=str(args.output), overall=report["overall"]["equal_game_mean"],
        failures=len(report["truth_support_failures"]))), flush=True)


if __name__ == "__main__":
    main()
