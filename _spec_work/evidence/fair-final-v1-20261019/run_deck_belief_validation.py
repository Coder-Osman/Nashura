import hashlib
from itertools import permutations
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
SOURCE = WORKSPACE / "firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.deck_belief import (DeckFrequency, FrozenDeckPrior,
    PublicCycleBelief, PublicDeckRecord, PublicPlay)
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.history_belief import compatible_opponent_draw
from native_runner.training.fair_search.belief import public_draw_compatible
from native_runner.training.fair_search.config import HOG_DECK
from verify_history_evidence import validate_history_evidence


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_histories():
    evidence = validate_history_evidence(ROOT, WORKSPACE)
    histories = []
    for relative in sorted(evidence["artifacts"]):
        name = Path(relative).name
        if name.startswith("history-") and name != "history-progress.json":
            histories.append((relative, FairHistory.from_payload(json.loads((ROOT / relative).read_text()))))
    if len(histories) != 12:
        raise ValueError("expected six verified stage roots per seat")
    return histories


def plays_of(history):
    return tuple(PublicPlay(event.tick, event.card_id) for event in history.deployments
        if event.owner != history.opening.owner)


def condition(state, history):
    delta = tuple(play for play in plays_of(history) if play.tick > state.tick)
    return state.advance(delta, from_tick=state.tick, to_tick=history.root_tick,
        expected_evidence_sha=state.evidence_sha)


def verify_roots(histories, prior):
    records = []
    for owner in (0, 1):
        selected = sorted((item for item in histories if item[1].opening.owner == owner), key=lambda item: item[1].root_tick)
        cursor = PublicCycleBelief(prior, owner=1-owner, opening_tick=selected[0][1].opening.tick)
        previous = None
        for relative, history in selected:
            if previous is not None and not history.extends(previous):
                raise ValueError("verified native history prefix changed")
            delta = condition(cursor, history)
            cold = PublicCycleBelief(prior, owner=1-owner, opening_tick=history.opening.tick)
            condition(cold, history)
            if cold.support_digest() != cursor.support_digest() or cold.evidence_sha != cursor.evidence_sha:
                raise ValueError("incremental card-cycle support differs from cold recomputation")
            cards = tuple(play.card_id for play in plays_of(history))
            expected = [draw for draw in permutations(sorted(HOG_DECK)) if public_draw_compatible(draw, cards)]
            if len(cursor._support) != 1 or cursor._support[0][1].tolist() != [list(draw) for draw in expected]:
                raise ValueError("card-cycle support differs from independent exhaustive reference")
            commitments = []
            for seed in range(16):
                completion = cursor.sample(seed=seed)
                if completion != cold.sample(seed=seed) or not public_draw_compatible(completion.initial_draw, cards):
                    raise ValueError("root draw replay or compatibility failed")
                if not public_draw_compatible(compatible_opponent_draw(history, seed), cards):
                    raise ValueError("legacy reference draw is not compatible")
                commitments.append(completion.manifest["completion_commitment"])
            records.append(dict(history_artifact=relative, history_sha=history.sha256, owner=owner,
                tick=history.root_tick, plays=len(cards), added_plays=delta["added_plays"],
                order_checks=delta["order_checks"], compatible_orders=len(expected),
                support_sha=cursor.support_digest(), evidence_sha=cursor.evidence_sha,
                memory_bytes=cursor.memory_bytes, sample_commitments=commitments, exact=True))
            previous = history
    return records


def benchmark(histories, prior, *, repeats=9):
    cases = {owner: sorted((history for _, history in histories if history.opening.owner == owner),
        key=lambda history: history.root_tick) for owner in (0, 1)}
    deltas = {}
    for owner, histories_for_owner in cases.items():
        tick = histories_for_owner[0].opening.tick
        for history in histories_for_owner:
            deltas[owner, history.root_tick] = tuple(play for play in plays_of(history) if play.tick > tick)
            tick = history.root_tick
    runs = []
    for repeat in range(repeats):
        order = ("legacy-cold", "incremental") if repeat % 2 == 0 else ("incremental", "legacy-cold")
        timings = {}
        for mode in order:
            roots, initializations = [], []
            for owner, histories_for_owner in cases.items():
                cursor = None
                if mode == "incremental":
                    started = time.perf_counter()
                    cursor = PublicCycleBelief(prior, owner=1-owner, opening_tick=histories_for_owner[0].opening.tick)
                    initializations.append(time.perf_counter()-started)
                for history in histories_for_owner:
                    started = time.perf_counter()
                    if mode == "legacy-cold":
                        compatible_opponent_draw(history, repeat)
                    else:
                        cursor.advance(deltas[owner, history.root_tick], from_tick=cursor.tick,
                            to_tick=history.root_tick, expected_evidence_sha=cursor.evidence_sha)
                        cursor.sample(seed=repeat)
                    roots.append(dict(owner=owner, tick=history.root_tick, seconds=time.perf_counter()-started))
            timings[mode] = dict(roots=roots, initialization_seconds=sum(initializations),
                total_seconds=sum(initializations)+sum(row["seconds"] for row in roots))
        runs.append(dict(repeat=repeat, order=list(order), timings=timings))
    medians = {mode: statistics.median(run["timings"][mode]["total_seconds"] for run in runs)
        for mode in ("legacy-cold", "incremental")}
    return dict(scope="local-card-cycle-filter-and-draw-only", repeats=repeats, runs=runs,
        medians=medians, median_total_ratio=medians["legacy-cold"]/medians["incremental"],
        includes="two incremental constructor costs and twelve sampled roots",
        excludes=["history-prefix-validation", "delta-extraction", "native-world-construction",
            "native-restore-or-reconditioning", "model-inference", "MCTS", "complete-games"])


def main():
    histories = load_histories()
    source_path = ROOT / "native-history-report.json"
    source_sha = sha(source_path)
    public_records = []
    for owner in (0, 1):
        latest = max((history for _, history in histories if history.opening.owner == 1-owner),
            key=lambda history: history.root_tick)
        public_records.append(PublicDeckRecord(source_sha, owner, plays_of(latest)))
    empirical = FrozenDeckPrior.from_public_records(public_records, source_sha=source_sha)
    if empirical.entries != (DeckFrequency(tuple(sorted(HOG_DECK)), 2),):
        raise ValueError("native public records did not reveal the expected fixed-deck scope")
    prior_path = ROOT / "public-deck-prior-engineering.json"
    prior_path.write_text(json.dumps(empirical.to_payload(), indent=2))
    records = verify_roots(histories, empirical)
    performance = benchmark(histories, empirical)
    files = [source_path, prior_path, Path(__file__),
        SOURCE / "native_runner/training/fair_final/deck_belief.py",
        SOURCE / "native_runner/tests/test_fair_final_deck_belief.py"]
    files.extend(ROOT / relative for relative, _ in histories)
    report = dict(schema="fair-final-deck-belief-evidence.v1", status="passed", training_admissible=False,
        prior_sha=empirical.sha256, source_game_groups=1, source_seat_records=2, source_unique_decks=1,
        source_scope="one-engineering-trajectory-not-population-calibration",
        records=records, benchmark=performance, identities={str(path.relative_to(WORKSPACE)): sha(path) for path in files},
        exclusions=["general-population-calibrated-prior", "exact-policy-weighted-posterior",
            "native-local-reconditioning", "native-search-integration", "training-admission"])
    target = ROOT / "deck-belief-report.json"
    target.write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(status="passed", roots=len(records), draws=16*len(records),
        source_seat_records=2, source_unique_decks=1, medians=performance["medians"],
        ratio=performance["median_total_ratio"], report=str(target))))


if __name__ == "__main__":
    main()
