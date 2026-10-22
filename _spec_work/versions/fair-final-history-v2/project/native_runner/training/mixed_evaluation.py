"""Paired native-game evaluation identities and four-deck protection tests."""
from __future__ import annotations

import random
from statistics import mean

from .event_opponents import digest
from .mixed_decks import DECKS, DECK_IDS, VARIANTS, RULES

VERSION = "mixed-deck-eval.v1"


def make_spec(*, candidate_sha, opponent_sha, deck, opponent_deck, suite, pair, owner,
              runtime_sha, action_constraints, seed_base):
    registry = {**DECKS, **VARIANTS}
    cell = f"{deck}:{opponent_deck}:{opponent_sha}"
    seed = int(digest([seed_base, suite, cell, pair])[:8], 16)
    spec = dict(version=VERSION, candidate_sha256=candidate_sha, opponent_sha256=opponent_sha,
                deck_id=deck, opponent_deck_id=opponent_deck, deck=list(registry[deck]),
                opponent_deck=list(registry[opponent_deck]), suite=suite, cell=cell,
                pair=pair, owner=owner, seed=seed, seed_base=seed_base, runtime_sha256=runtime_sha,
                rules=RULES, action_constraints=action_constraints)
    return dict(spec, sha256=digest(spec))


def validate_spec(spec):
    expected = make_spec(candidate_sha=spec["candidate_sha256"], opponent_sha=spec["opponent_sha256"],
        deck=spec["deck_id"], opponent_deck=spec["opponent_deck_id"], suite=spec["suite"],
        pair=spec["pair"], owner=spec["owner"], runtime_sha=spec["runtime_sha256"],
        action_constraints=spec["action_constraints"], seed_base=spec["seed_base"])
    if expected != spec or spec["owner"] not in (0, 1) or spec["pair"] < 0:
        raise ValueError("evaluation specification mismatch")
    if spec["action_constraints"].get("delay") != {"minimum_ms": 100}:
        raise ValueError("evaluation requires 100ms contract")
    for name in ("candidate_sha256", "opponent_sha256", "runtime_sha256"):
        if len(spec[name]) != 64 or any(c not in "0123456789abcdef" for c in spec[name]):
            raise ValueError("evaluation SHA invalid")
    return spec


def match_metadata(spec, port):
    owner = spec["owner"]
    decks = [spec["opponent_deck_id"]] * 2
    cards = [spec["opponent_deck"]] * 2
    identities = [spec["opponent_sha256"]] * 2
    decks[owner], cards[owner], identities[owner] = spec["deck_id"], spec["deck"], spec["candidate_sha256"]
    return dict(version=VERSION, kind="evaluation", learner_owner=owner,
                owner_deck_ids=decks, owner_decks=cards, owner_sha256=identities,
                opponent=dict(sha256=spec["opponent_sha256"]), port=port, seed=spec["seed"],
                game_id=spec["sha256"], rules=RULES, action_constraints=spec["action_constraints"])


def matrix_cells(baseline_sha, fixed=(), *, variants=False):
    decks = tuple(VARIANTS) if variants else DECK_IDS
    cells = [(a, b, baseline_sha) for a in decks for b in DECK_IDS]
    cells += [("golem", "golem", r["sha256"]) for r in fixed]
    return cells


def specs_for(candidate_sha, cells, pairs, *, suite, runtime_sha, action_constraints, seed_base):
    return [make_spec(candidate_sha=candidate_sha, opponent_sha=sha, deck=a, opponent_deck=b,
                      suite=suite, pair=p, owner=o, runtime_sha=runtime_sha,
                      action_constraints=action_constraints, seed_base=seed_base)
            for a, b, sha in cells for p in range(pairs) for o in (0, 1)]


def paired_scores(records):
    pairs = {}
    for record in records:
        spec = validate_spec(record["spec"])
        if not record.get("true_terminal") or record["score"] not in (0., .5, 1.):
            raise ValueError("evaluation needs a true terminal score")
        key = (spec["cell"], spec["pair"])
        owners = pairs.setdefault(key, {})
        if spec["owner"] in owners:
            raise ValueError("duplicate evaluation side")
        owners[spec["owner"]] = record["score"]
    if not pairs or any(set(owners) != {0, 1} for owners in pairs.values()):
        raise ValueError("evaluation requires complete swapped seed pairs")
    return {key: mean(owners.values()) for key, owners in pairs.items()}


def bootstrap(groups, *, samples=4000, seed=729431):
    """Equal-cell stratified bootstrap; each observation is a whole seed pair."""
    if not groups or any(not values for values in groups):
        raise ValueError("bootstrap needs nonempty cells")
    rng = random.Random(seed)
    estimates = sorted(mean(mean(rng.choices(values, k=len(values))) for values in groups)
                       for _ in range(samples))
    return [estimates[int(.025 * samples)], estimates[min(samples - 1, int(.975 * samples))]]


def comparison(candidate_records, baseline_records, baseline_sha):
    candidate, baseline = paired_scores(candidate_records), paired_scores(baseline_records)
    if candidate.keys() != baseline.keys():
        raise ValueError("paired comparison sample mismatch")
    left = {(r["spec"]["cell"], r["spec"]["pair"], r["spec"]["owner"]): r["spec"] for r in candidate_records}
    for record in baseline_records:
        spec = record["spec"]
        actual = left[(spec["cell"], spec["pair"], spec["owner"])]
        if any(actual[k] != spec[k] for k in ("suite", "seed", "runtime_sha256", "rules", "action_constraints")):
            raise ValueError("paired comparison seed/runtime/contract mismatch")
    cells = sorted({key[0] for key in candidate})
    details = {}
    for cell in cells:
        keys = sorted(k for k in candidate if k[0] == cell)
        values = [candidate[k] for k in keys]
        differences = [candidate[k] - baseline[k] for k in keys]
        records = [r for r in candidate_records if r["spec"]["cell"] == cell]
        details[cell] = dict(pairs=len(keys), games=len(records),
            wins=sum(r["score"] == 1. for r in records), draws=sum(r["score"] == .5 for r in records),
            losses=sum(r["score"] == 0. for r in records), score_rate=mean(values),
            baseline_score_rate=mean(baseline[k] for k in keys), delta=mean(differences),
            score_ci95=bootstrap([values]), delta_ci95=bootstrap([differences]))

    def group(selected):
        if not selected:
            return None
        differences = [[candidate[k] - baseline[k] for k in sorted(candidate) if k[0] == cell]
                       for cell in selected]
        return dict(cells=selected, score_rate=mean(details[c]["score_rate"] for c in selected),
                    baseline_score_rate=mean(details[c]["baseline_score_rate"] for c in selected),
                    delta=mean(map(mean, differences)), delta_ci95=bootstrap(differences))

    mirror = group([c for c in cells if c.split(":")[:2] == ["golem", "golem"]])
    cross = group([c for c in cells if c.split(":")[0] == "golem" and c.split(":")[1] != "golem"])
    average = group([c for c in cells if c.split(":")[2] == baseline_sha])
    return dict(cells=details, golem_mirror=mirror, golem_cross=cross, equal_deck_average=average,
                score_definition="(wins + 0.5 * draws) / games", interval="paired_seed_stratified_bootstrap_95")


def protection(report, *, stage_boundary, at_limit=False):
    groups = [report["golem_mirror"], report["golem_cross"]]
    if any(g is None for g in groups):
        raise ValueError("protection requires mirror and cross-deck evidence")
    passed = all(g["delta_ci95"][0] >= -.05 for g in groups)
    regression = any(g["delta_ci95"][1] < -.05 for g in groups)
    triggered = stage_boundary or any(g["delta"] < -.05 for g in groups)
    return dict(passed=passed, confirmed_regression=regression,
                expand=triggered and not passed and not regression and not at_limit,
                stop=regression or (stage_boundary and at_limit and not passed))
