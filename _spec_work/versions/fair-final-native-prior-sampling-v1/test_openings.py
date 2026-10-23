from copy import deepcopy
from itertools import permutations
import random

import pytest

from prior_sampling import SAMPLE_COUNT, diagnose, seeds, validate_pair, validate_rows


DECK = tuple(range(100, 108))


def fixture_rows():
    rng = random.Random(20261023)
    rows = []
    for ordinal, seed in enumerate(seeds()):
        players = []
        for owner in (0, 1):
            slots = list(range(8))
            rng.shuffle(slots)
            players.append(dict(owner=owner, slots=slots, cards=[DECK[slot] for slot in slots]))
        rows.append(dict(ordinal=ordinal, seed=seed, tick=90, players=players))
    return rows


def test_preregistered_seeds():
    assert len(set(seeds())) == SAMPLE_COUNT
    assert seeds() == seeds()
    assert all(0 <= seed < 2**32 for seed in seeds())


def test_uniform_reference_is_limited():
    report = diagnose(fixture_rows(), seeds(), DECK)
    assert len(report["tests"]) == 21
    assert report["bonferroni_threshold"] == .01 / 21
    assert report["status"] == "no_detected_departure_limited_scope"
    assert report["exact_uniformity_proven"] is False
    assert report["gates_closed"] is False


def test_biased_draws_rejected():
    rows = fixture_rows()
    for row in rows:
        for player in row["players"]:
            player.update(slots=list(range(8)), cards=list(DECK))
    assert diagnose(rows, seeds(), DECK)["status"] == "uniform_prior_rejected"


@pytest.mark.parametrize("field,value", [("seed", 0), ("ordinal", True), ("tick", 91)])
def test_row_identity_rejected(field, value):
    rows = fixture_rows()
    rows[0][field] = value
    with pytest.raises(ValueError):
        validate_rows(rows, seeds(), DECK)


@pytest.mark.parametrize("field,value", [("owner", True), ("slots", list(range(7)) + [6]),
    ("cards", list(DECK)[::-1])])
def test_player_support_rejected(field, value):
    rows = fixture_rows()
    rows[0]["players"][0][field] = value
    with pytest.raises(ValueError):
        validate_rows(rows, seeds(), DECK)


def test_cross_process_pair():
    rows = fixture_rows()
    assert validate_pair(rows, deepcopy(rows[:64]), DECK)["cross_process_openings_equal"]
    repeated = deepcopy(rows[:64])
    repeated[0]["players"] = deepcopy(rows[1]["players"])
    with pytest.raises(ValueError, match="across fresh"):
        validate_pair(rows, repeated, DECK)


def test_partial_run_rejected():
    with pytest.raises(ValueError):
        diagnose(fixture_rows()[:128], seeds()[:128], DECK)


def test_full_permutation_reference():
    counts = [0] * 8
    for draw in permutations(range(8)):
        counts[draw[0]] += 1
    assert counts == [5040] * 8
