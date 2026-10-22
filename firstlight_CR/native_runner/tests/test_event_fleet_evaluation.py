import pytest

from tools.event_rl_fleet_evaluate import parse_match, summarize


def events(owner, results, terminal=True):
    return [
        {"event": "checkpoint-loaded", "role": "learner",
         "checkpoint_sha256": "rl" if owner == 0 else "il", "contract_sha256": "v2"},
        {"event": "checkpoint-loaded", "role": "opponent",
         "checkpoint_sha256": "il" if owner == 0 else "rl", "contract_sha256": "v2"},
        {"match": "test", "terminal": terminal, "results": results},
    ]


@pytest.mark.parametrize("owner,results,reward", [
    (0, {"0": 1, "1": -1}, 1),
    (1, {"0": 1, "1": -1}, -1),
    (1, {"0": -1, "1": 1}, 1),
    (0, {"0": 0, "1": 0}, 0),
])
def test_results_follow_rl_side(owner, results, reward):
    result = parse_match(events(owner, results), rl_owner=owner, rl_sha="rl", opponent_sha="il")
    assert result["reward"] == reward
    assert result["score"] == (reward + 1) / 2


def test_unfinished_match_is_not_a_draw():
    with pytest.raises(ValueError, match="complete terminal"):
        parse_match(events(0, {"0": 0, "1": 0}, terminal=False),
                    rl_owner=0, rl_sha="rl", opponent_sha="il")


def test_wrong_checkpoint_and_shaped_reward_rejected():
    with pytest.raises(ValueError, match="checkpoint mismatch"):
        parse_match(events(1, {"0": 1, "1": -1}), rl_owner=0, rl_sha="rl", opponent_sha="il")
    with pytest.raises(ValueError, match="unshaped"):
        parse_match(events(0, {"0": 1.1, "1": -1.1}), rl_owner=0, rl_sha="rl", opponent_sha="il")


def test_pair_bootstrap_preserves_side_bias():
    # A pure owner-0 advantage must produce exactly 50% in every resampled pair.
    records = [{"pair_id": pair, "rl_owner": owner, "reward": 1 if owner == 0 else -1,
                "score": 1 if owner == 0 else 0} for pair in range(6) for owner in (0, 1)]
    report = summarize(records, 6)
    assert (report["wins"], report["draws"], report["losses"]) == (6, 0, 6)
    assert report["score_rate_95_ci"] == [0.5, 0.5]
    assert report["by_rl_owner"]["0"]["win_rate"] == 1
    assert report["by_rl_owner"]["1"]["win_rate"] == 0
    assert report["interpretation"] == "inconclusive"
    with pytest.raises(ValueError, match="complete pairs"):
        summarize(records[:-1], 6)
    with pytest.raises(ValueError, match="duplicate"):
        summarize(records + records[:1], 6)


def test_draws_count_as_half_points():
    records = [{"pair_id": 0, "rl_owner": 0, "reward": 1, "score": 1},
               {"pair_id": 0, "rl_owner": 1, "reward": 0, "score": 0.5}]
    report = summarize(records, 1)
    assert report["win_rate"] == 0.5
    assert report["score_rate"] == 0.75
