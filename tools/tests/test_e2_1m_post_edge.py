from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from run_e2_observed import post_edge_lifecycle


@pytest.mark.parametrize('failure', [None, 'branch', 'terminal', 'actions', 'queue'])
def test_post_edge_lifecycle_evidence_and_failures(failure):
    calls, saved, pulled = [], {}, []
    snapshots = iter([dict(handle=1, digest='initial', bytes=20),
                      dict(handle=2, digest='branch', bytes=21),
                      dict(handle=3, digest='different' if failure == 'branch' else 'branch', bytes=21)])

    def control(command, phase):
        calls.append((command, phase))
        if command == 'snapshot-create':
            return next(snapshots)
        if command.startswith('standalone-trace '):
            return dict(finalized=failure != 'terminal', injected=27 if failure == 'actions' else 28,
                        queue_count=1 if failure == 'queue' else 0, snapshot_directory='/exclusive/snapshots')
        return dict(ok=True)

    if failure:
        with pytest.raises(RuntimeError, match='post-edge'):
            post_edge_lifecycle(control, pulled.append, saved.__setitem__, {}, 'fixed-actions')
        assert not pulled
        assert ('reset', 'post_edge_terminal_reset_destroy') not in calls
    else:
        post_edge_lifecycle(control, pulled.append, saved.__setitem__, {}, 'fixed-actions')
        assert pulled == ['/exclusive/snapshots']
        assert saved['post-edge-trace.json']['injected'] == 28
        assert len(calls) == 17
        assert calls[-3:] == [('observe', 'post_edge_terminal'), ('reset', 'post_edge_terminal_reset_destroy'),
                              ('observe', 'post_edge_final_reset_state')]
    assert all(phase.startswith('post_edge_') for _, phase in calls)
