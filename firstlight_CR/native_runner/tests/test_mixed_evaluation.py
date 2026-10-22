from copy import deepcopy

import pytest

from native_runner.training.mixed_evaluation import (
    specs_for, matrix_cells, validate_spec, match_metadata, paired_scores, comparison, protection,
)

BASELINE = "a" * 64
CURRENT = "b" * 64


def specs(sha=CURRENT, pairs=10, variants=False):
    return specs_for(sha, matrix_cells(BASELINE, [dict(sha256="c" * 64), dict(sha256="d" * 64)] if not variants else (),
                                       variants=variants), pairs,
                     suite="test", runtime_sha="e" * 64, action_constraints={"delay": {"minimum_ms": 100}}, seed_base=123)


def test_360_games_seed_pairs_and_heldout_decks():
    current, baseline = specs(), specs(BASELINE)
    assert len(current) == 360
    assert len({s["sha256"] for s in current}) == 360
    assert [s["seed"] for s in current] == [s["seed"] for s in baseline]
    for a, b in zip(current[::2], current[1::2]):
        assert a["seed"] == b["seed"] and a["pair"] == b["pair"]
        assert match_metadata(a, 1)["owner_decks"] == match_metadata(b, 1)["owner_decks"][::-1]
        assert validate_spec(a) == a
    assert len(specs(pairs=25, variants=True)) == 800


def test_pair_integrity_and_protection_expansion_stop():
    candidate = [dict(spec=s, score=.5, true_terminal=True) for s in specs()]
    baseline = [dict(spec=s, score=.5, true_terminal=True) for s in specs(BASELINE)]
    report = comparison(candidate, baseline, BASELINE)
    assert report["equal_deck_average"]["delta_ci95"] == [0., 0.]
    assert protection(report, stage_boundary=True)["passed"]
    with pytest.raises(ValueError, match="complete swapped"):
        paired_scores(candidate[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        paired_scores(candidate + candidate[:1])
    uncertain = deepcopy(report)
    uncertain["golem_cross"].update(delta=-.01, delta_ci95=[-.08, .04])
    assert protection(uncertain, stage_boundary=True)["expand"]
    assert protection(uncertain, stage_boundary=True, at_limit=True)["stop"]
    assert not protection(uncertain, stage_boundary=False)["expand"]
    uncertain["golem_cross"]["delta"] = -.06
    assert protection(uncertain, stage_boundary=False)["expand"]
    uncertain["golem_cross"]["delta_ci95"] = [-.1, -.06]
    assert protection(uncertain, stage_boundary=False)["stop"]


def test_equal_cell_mean_is_not_changed_by_golem_expansion():
    candidate_specs, baseline_specs = specs(pairs=2), specs(BASELINE, pairs=2)
    base_cells = matrix_cells(BASELINE)
    golem = [c for c in base_cells if c[0] == "golem"]
    extra_args = dict(pairs=4, suite="test", runtime_sha="e" * 64,
                      action_constraints={"delay": {"minimum_ms": 100}}, seed_base=123)
    for sha, target in ((CURRENT, candidate_specs), (BASELINE, baseline_specs)):
        target.extend(s for s in specs_for(sha, golem, **extra_args) if s["pair"] >= 2)
    candidate = [dict(spec=s, score=1. if s["deck_id"] == "golem" else 0., true_terminal=True) for s in candidate_specs]
    baseline = [dict(spec=s, score=.5, true_terminal=True) for s in baseline_specs]
    report = comparison(candidate, baseline, BASELINE)
    assert report["equal_deck_average"]["score_rate"] == .25
