import gzip
import importlib.util
import json
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).parents[1]))
SPEC = importlib.util.spec_from_file_location("verify_headless_traces", Path(__file__).parents[1] / "verify_headless_traces.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def write_trace(path, base=1, damage=0, complete=True):
    with gzip.open(path, "wt") as output:
        for tick in (0, 100):
            value = {"generation": base, "stateEpoch": base, "tick": tick,
                     "ordinary": {"tick": tick, "ended": tick == 100 and complete,
                                  "finalized": tick == 100 and complete,
                                  "queuedCommands": 0, "tower_hp": 1000 - damage},
                     "rich": {"combatEvents": {"epochFirstSequence": base,
                                  "events": [{"sequence": base, "causeSequence": None}],
                                  "droppedCount": 0}}}
            output.write(json.dumps(value) + "\n")


def test_real_trace_verifier_rebases_only_identity(tmp_path):
    left, right = tmp_path / "left.gz", tmp_path / "right.gz"
    write_trace(left)
    write_trace(right, base=50)
    result = MODULE.verify(left, right)
    assert result["status"] == "passed"
    assert result["boundary_count"] == 2
    assert result["training_admissible"] is False


@pytest.mark.parametrize("damage,complete", [(1, True), (0, False)])
def test_real_trace_verifier_rejects_state_difference_and_truncation(tmp_path, damage, complete):
    left, right = tmp_path / "left.gz", tmp_path / "right.gz"
    write_trace(left, complete=complete)
    write_trace(right, damage=damage, complete=complete)
    with pytest.raises(ValueError):
        MODULE.verify(left, right)
