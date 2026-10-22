import math
import random


def paired_confidence(scores, *, seed=440001, resamples=10000):
    if not scores or any(len(pair) != 2 or any(value not in (0., .5, 1.) for value in pair) for pair in scores):
        raise ValueError("evaluation requires complete owner-swapped score pairs")
    means = [(pair[0] + pair[1]) / 2 for pair in scores]
    rng = random.Random(seed)
    bootstrapped = sorted(sum(rng.choices(means, k=len(means))) / len(means) for _ in range(resamples))
    return dict(games=2 * len(scores), score=sum(means) / len(means),
                lower95=bootstrapped[int(.025 * resamples)],
                upper95=bootstrapped[min(resamples - 1, math.ceil(.975 * resamples))])


def promotion_decision(screen_pairs, confirmation_pairs):
    screen = paired_confidence(screen_pairs)
    if screen["games"] != 400:
        raise ValueError("promotion screening requires 400 games")
    if screen["score"] <= .5:
        return dict(promote=False, screen=screen, reason="screen-not-improved")
    confirmation = paired_confidence(confirmation_pairs)
    if confirmation["games"] != 2000:
        raise ValueError("promotion confirmation requires 2000 independent games")
    return dict(promote=confirmation["lower95"] > .5, screen=screen,
                confirmation=confirmation, reason="paired-bootstrap-confirmation")


def final_acceptance(pairs):
    result = paired_confidence(pairs, seed=440002)
    if result["games"] != 2000:
        raise ValueError("final held-out evaluation requires 2000 games")
    return dict(passed=result["score"] >= .55 and result["lower95"] > .5, **result)
