"""Local contract checks; no engine or remote process is started."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from distillation import TeacherRecord, distillation_loss, scheduled_weights, split_episodes  # noqa: E402
from plan import build_plan  # noqa: E402
from tree_search import SearchState, aggregate_opponent_values, q_target, state_digest  # noqa: E402


def main() -> None:
    config = json.loads((ROOT / "config.json").read_text())
    plan = build_plan(config)
    assert plan["learner_deck"] == [26000021, 26000038, 26000014, 26000010, 26000030, 27000000, 28000000, 28000011]
    state = SearchState(
        native_snapshot={"tick": 10, "snapshot": "fixture"}, observation={"tick": 10}, actor=0,
        legal_actions=("wait", "hog-left"), external_queue=(), reward_ledger={"outcome": 0.0},
        event_cursor={"sequence": 0}, policy_state={"hidden": [0.0]},
    )
    assert SearchState.from_dict(state.to_dict()) == state
    assert state_digest(state) == state_digest(SearchState.from_dict(state.to_dict()))
    expected, robust = aggregate_opponent_values([
        type("R", (), {"probability": 0.75, "value": 0.4})(),
        type("R", (), {"probability": 0.25, "value": -0.2})(),
    ])
    assert abs(q_target(expected, robust) - (0.7 * expected + 0.3 * robust)) < 1e-9
    records = []
    for episode in range(10):
        records.append(TeacherRecord(
            episode_id=f"e{episode}", decision_index=0, observation={"tick": episode},
            legal_actions=("wait", "hog-left"), visit_counts={"wait": 32, "hog-left": 96},
            teacher_policy={"wait": .25, "hog-left": .75}, q_expected=.2, q_robust=.1,
            q_target=.17, terminal_result=1.0, opponent_sha256="a" * 64,
            search_simulations=128, teacher_temperature=1.0, opponent_temperature=.5,
            state_mode="deterministic_full_state", seed=episode,
            native_runtime_sha256="b" * 64, content_pack_sha256="c" * 64))
    split = split_episodes(records)
    assert sum(map(len, split.values())) == len(records)
    assert all(len({r.episode_id for r in rows}) == len(rows) for rows in split.values())
    assert scheduled_weights(0) == (.5, .1) and scheduled_weights(200) == (.25, .05)
    import torch
    total, ce, huber, _ = distillation_loss(
        torch.log_softmax(torch.tensor([[1.0, .0]]), -1), torch.tensor([[.25, .75]]),
        torch.tensor([.1]), torch.tensor([.2]), policy_weight=.5, value_weight=.1)
    assert all(torch.isfinite(x) for x in (total, ce, huber))
    print(json.dumps({"passed": True, "schema": "hog-tree-search-validation.v1",
                      "plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()
