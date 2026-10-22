from copy import deepcopy
from types import SimpleNamespace

import pytest

from verify_prior_search import validate_attempts


def shallow_entry():
    transition = dict(before_tick=100, before_sha="a"*64, hard_tick=360,
        maximum_decisions=12, completed=True, ticks=2, decisions=1,
        reason="preempted", terminal=None, after_sha="b"*64, after_tick=102)
    return dict(tick=100, fair_sha="a"*64,
        attempts=[dict(ticks=2, decisions=1, transitions=[deepcopy(transition)]) for index in range(128)],
        audit=dict(attempts=128, discarded_edges=0, completed_lifecycle=dict(preempted=128)),
        outcome=dict(result=dict(invalid_attempts=0)),
        world_lifecycle=dict(builds=1, restores=130, closed=True, snapshot_released=True, root_digest="c"*64))


def limits():
    return SimpleNamespace(soft_decisions=6, soft_ticks=200, extension_decisions=6, extension_ticks=60)


def test_shallow_completed_search_has_exact_budget_and_restoration():
    validate_attempts(shallow_entry(), limits(), require_depth_coverage=False)


def test_historical_depth_coverage_requirement_remains_default():
    with pytest.raises(ValueError, match="budget or lifecycle"):
        validate_attempts(shallow_entry(), limits())


@pytest.mark.parametrize("case", ["missing_attempt", "invalid_budget", "wrong_lifecycle", "wrong_discarded",
    "broken_chain", "unfinished_root", "wrong_hard_limit", "fewer_restores", "snapshot_leak", "extra_world"])
def test_shallow_search_still_rejects_invalid_evidence(case):
    entry = shallow_entry()
    transition = entry["attempts"][0]["transitions"][0]
    if case == "missing_attempt":
        entry["attempts"].pop()
    elif case == "invalid_budget":
        entry["outcome"]["result"]["invalid_attempts"] = 1
    elif case == "wrong_lifecycle":
        entry["audit"]["completed_lifecycle"]["preempted"] -= 1
    elif case == "wrong_discarded":
        entry["audit"]["discarded_edges"] = 1
    elif case == "broken_chain":
        transition["before_sha"] = "d"*64
    elif case == "unfinished_root":
        transition.update(completed=False, after_sha="a"*64, after_tick=100)
        entry["attempts"][0].update(ticks=0, decisions=0)
        entry["audit"].update(discarded_edges=1, completed_lifecycle=dict(preempted=127))
        entry["outcome"]["result"]["invalid_attempts"] = 1
    elif case == "wrong_hard_limit":
        transition["hard_tick"] -= 1
    elif case == "fewer_restores":
        entry["world_lifecycle"]["restores"] -= 1
    elif case == "snapshot_leak":
        entry["world_lifecycle"]["snapshot_released"] = False
    elif case == "extra_world":
        entry["world_lifecycle"]["builds"] = 2
    with pytest.raises(ValueError):
        validate_attempts(entry, limits(), require_depth_coverage=False)
