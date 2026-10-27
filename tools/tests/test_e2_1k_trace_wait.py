from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_e2_observed import wait_path_trace


def test_cold_ready_does_not_imply_natural_write_finished():
    states = iter([dict(started=False, stopped=False), dict(started=True, stopped=False),
                   dict(started=True, stopped=True)])
    receipts, pauses = [], []
    wait_path_trace(lambda: dict(path_trace=next(states), cold=dict(errors=[])),
                    lambda name, state: receipts.append((name, state)),
                    clock=lambda: 0, pause=pauses.append)
    assert len(receipts) == 3 and pauses == [0.1, 0.1]


def test_trace_wait_is_bounded_and_retains_last_receipt():
    times = iter([0, 0, 31])
    receipts = []
    with pytest.raises(RuntimeError, match='bounded wait'):
        wait_path_trace(lambda: dict(path_trace=dict(started=True, stopped=False), cold=dict(errors=[])),
                        lambda name, state: receipts.append(state), clock=lambda: next(times), pause=lambda _: None)
    assert receipts == [dict(started=True, stopped=False)] * 2


def test_trace_observer_error_is_not_retried_as_success():
    with pytest.raises(RuntimeError, match='observer failed'):
        wait_path_trace(lambda: dict(path_trace=dict(started=True, stopped=True), cold=dict(errors=['failed'])),
                        lambda *_: None)
