from copy import deepcopy
import hashlib
import json

import pytest

from freeze_headless_actions import freeze
from test_run_headless_match import COMPLETE


@pytest.fixture
def recording():
    replay = dict(cmd=[], inject=[], evt=[], rndSeed=7,
                  battle={f"deck{owner}": dict(sp=[dict(d=26000000 + i) for i in range(8)])
                          for owner in (0, 1)})
    source = json.dumps(replay).encode()
    sha = hashlib.sha256(source).hexdigest()
    plan = dict(command=["runtime", "/oldroot/replay.json", sha],
                identities={"/replay.json": sha}, record_hand_actions=True,
                benchmark_repeats=None, environment={"NASHURA_DEPLOY_HAND": "1"})
    lines = []
    for owner in (0, 1):
        packed = (3 << 28) | (1 << 22)
        action = dict(ct=86, c=dict(t=0, t2=20, idHi=0, idLo=owner + 1,
                                   sel=dict(os=26000000, pd=packed)))
        lines.append(f"HEADLESS_BATTLE_DEPLOY_QUEUED tick=0 owner={owner} slot=0 "
                     f"card=26000000 packed={packed} cost=3 elixir=50000 "
                     f"queue_before={owner} queue_after={owner+1} json={json.dumps(action)}")
    return source, "\n".join(lines) + "\n" + COMPLETE, plan


def test_freezes_exact_native_json_and_preserves_input(recording):
    source, log, plan = recording
    original = json.loads(source)
    result, summary = freeze(source, log, 0, plan)
    assert len(result["cmd"]) == summary["command_count"] == 2
    assert result["cmd"][0] == json.loads(log.splitlines()[0].split(" json=", 1)[1])
    assert result["battle"] == original["battle"]
    assert result["rndSeed"] == 7
    assert json.loads(source)["cmd"] == []
    assert summary["recorded_final_tick"] == 3601
    assert summary["whole_goal_complete"] is False


@pytest.mark.parametrize("damage", ["identity", "benchmark", "not-recording", "mixed", "duplicate-card"])
def test_rejects_wrong_source_or_plan(recording, damage):
    source, log, plan = recording
    plan = deepcopy(plan)
    if damage == "identity":
        plan["command"][-1] = "0" * 64
    elif damage == "benchmark":
        plan["benchmark_repeats"] = 20
    elif damage == "not-recording":
        plan["record_hand_actions"] = False
    else:
        replay = json.loads(source)
        if damage == "mixed":
            replay["inject"] = [dict(ct=86)]
        else:
            replay["battle"]["deck0"]["sp"][1] = replay["battle"]["deck0"]["sp"][0]
        source = json.dumps(replay).encode()
        sha = hashlib.sha256(source).hexdigest()
        plan["command"][-1] = plan["identities"]["/replay.json"] = sha
    with pytest.raises(ValueError):
        freeze(source, log, 0, plan)


@pytest.mark.parametrize("before,after", [
    ("slot=0", "slot=8"), ("cost=3", "cost=4"),
    ("elixir=50000", "elixir=20000"), ("queue_after=1", "queue_after=0"),
    ('"t": 0', '"t": 1'), ('"os": 26000000', '"os": 26000007'),
    ("tick=0 owner=1", "tick=0 owner=0"),
    ("HEADLESS_BATTLE_DEPLOY_QUEUED tick=0 owner=0", "HEADLESS_BATTLE_DEPLOY_QUEUED broken"),
])
def test_rejects_inconsistent_native_metadata(recording, before, after):
    source, log, plan = recording
    with pytest.raises(ValueError):
        freeze(source, log.replace(before, after), 0, plan)


def test_requires_normal_completion_and_actions_for_both_players(recording):
    source, log, plan = recording
    with pytest.raises(ValueError, match="exit code"):
        freeze(source, log, 124, plan)
    with pytest.raises(ValueError, match="both players"):
        freeze(source, COMPLETE, 0, plan)
    with pytest.raises(ValueError, match="both players"):
        freeze(source, "\n".join(line for line in log.splitlines() if "owner=1" not in line), 0, plan)
