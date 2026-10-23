import hashlib
import json
from collections import Counter
from itertools import combinations, permutations

SAMPLE_COUNT = 1024
REPEAT_COUNT = 64
PORT = 29777
FAMILY_ALPHA = .01
PROBE_SHA = "49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba"


def sha(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def seeds():
    result = [int.from_bytes(hashlib.sha256(
        f"Nashura-G0G1-opening-prior-20261023:{index}".encode()).digest()[:4], "big")
        for index in range(SAMPLE_COUNT)]
    if len(set(result)) != SAMPLE_COUNT:
        raise ValueError("predeclared opening seeds collide")
    return result


def validate_rows(rows, expected_seeds, deck):
    if len(deck) != 8 or len(set(deck)) != 8 or len(rows) != len(expected_seeds):
        raise ValueError("opening sample count or deck differs")
    for ordinal, (row, seed) in enumerate(zip(rows, expected_seeds)):
        if (set(row) != {"ordinal", "seed", "tick", "players"}
                or type(row["ordinal"]) is not int or row["ordinal"] != ordinal
                or type(row["seed"]) is not int or row["seed"] != seed or row["tick"] != 90
                or len(row["players"]) != 2):
            raise ValueError("opening row identity, seed, or tick differs")
        for owner, player in enumerate(row["players"]):
            if (set(player) != {"owner", "cards", "slots"} or type(player["owner"]) is not int
                    or player["owner"] != owner or len(player["cards"]) != 8 or len(player["slots"]) != 8
                    or any(type(value) is not int for value in player["cards"] + player["slots"])
                    or set(player["slots"]) != set(range(8)) or set(player["cards"]) != set(deck)
                    or player["cards"] != [deck[slot] for slot in player["slots"]]):
                raise ValueError("native opening is outside declared permutation support")


def statistic(name, samples, support):
    import numpy as np
    from scipy.stats import chisquare

    counts = Counter(samples)
    if set(counts) - set(support):
        raise ValueError("observed opening outside statistic support")
    observed = np.array([counts[value] for value in support], dtype=np.int64)
    expected = len(samples) / len(support)
    if expected < 5:
        raise ValueError("opening categorical expected count too small")
    test = chisquare(observed)
    return dict(name=name, categories=len(support), samples=len(samples),
        expected_per_category=expected, counts=observed.tolist(),
        chi_square=float(test.statistic), p_value=float(test.pvalue))


def diagnose(rows, expected_seeds, deck):
    validate_rows(rows, expected_seeds, deck)
    if len(rows) != SAMPLE_COUNT:
        raise ValueError("formal opening diagnostic requires preregistered sample count")
    tests = []
    for owner in (0, 1):
        draws = [row["players"][owner]["slots"] for row in rows]
        for position in range(8):
            tests.append(statistic(f"seat-{owner}-position-{position}",
                [draw[position] for draw in draws], list(range(8))))
        tests.append(statistic(f"seat-{owner}-unordered-hand",
            [tuple(sorted(draw[:4])) for draw in draws], list(combinations(range(8), 4))))
        tests.append(statistic(f"seat-{owner}-first-two",
            [tuple(draw[:2]) for draw in draws], list(permutations(range(8), 2))))
    tests.append(statistic("cross-seat-first-card", [tuple(row["players"][owner]["slots"][0]
        for owner in (0, 1)) for row in rows], [(first, second) for first in range(8) for second in range(8)]))
    threshold = FAMILY_ALPHA / len(tests)
    for test in tests:
        test["rejected"] = test["p_value"] < threshold
    return dict(schema="fair-final-native-opening-distribution.v1",
        status="uniform_prior_rejected" if any(test["rejected"] for test in tests)
        else "no_detected_departure_limited_scope", samples=len(rows),
        unique_orders_per_seat=[len({tuple(row["players"][owner]["slots"]) for row in rows})
            for owner in (0, 1)], null="independent uniform eight-card permutations per seed and seat",
        family_alpha=FAMILY_ALPHA, bonferroni_threshold=threshold, tests=tests,
        exact_uniformity_proven=False, all_permutations_observed=False,
        history_conditional_calibration_verified=False, training_admissible=False, gates_closed=False)


def validate_pair(first, second, deck):
    validate_rows(first, seeds(), deck)
    validate_rows(second, seeds()[:REPEAT_COUNT], deck)
    if first[:REPEAT_COUNT] != second:
        raise ValueError("same-seed opening differs across fresh native processes")
    return dict(repeated_seeds=REPEAT_COUNT, cross_process_openings_equal=True)


def read_rows(path):
    with open(path) as handle:
        return [json.loads(line) for line in handle]
