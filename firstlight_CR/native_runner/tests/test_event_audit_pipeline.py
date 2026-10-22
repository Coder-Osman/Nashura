import json
from pathlib import Path
import threading

import pytest
from tools import event_rl_round_audit as audit


@pytest.fixture
def pipeline(tmp_path,monkeypatch):
    monkeypatch.setattr(audit, 'V4', {'version':'test-settlement'})
    plan=dict(matches=[dict(host='37',port=1,game_id='a'),dict(host='37',port=2,game_id='b')],
        current_sha256='s',expected_games=2,expected_player_lanes=3,reward_config=audit.V4)
    (tmp_path/'match-plan.json').write_text(json.dumps(plan))
    monkeypatch.setattr(audit,'validate_plan',lambda value:value)
    monkeypatch.setattr(audit,'validate_round',lambda *a,**k:dict(trainable_rows=7,excluded_rows=2,passed=True))
    versions={1:'sha1',2:'sha2'}
    monkeypatch.setattr(audit,'evidence_digest',lambda p, *, settlement=False:dict(digest=versions[int(p.name.split('-')[1])]))
    calls=[]
    def check(folder,match,plan,cache):
        calls.append(match['port'])
        return dict(totals=dict(lanes=2 if match['port']==1 else 1,effective_decisions=4 if match['port']==1 else 3,cancelled_decisions=1),
            components=dict(settlement_telescoping_error_max=0.), diagnostics={},traces={},terminal_reconciliations={},evidence=audit.evidence_digest(folder))
    monkeypatch.setattr(audit,'audit_match',check)
    return tmp_path,versions,calls


def test_early_queue_duplicate_reuse_and_complete_barrier(pipeline):
    directory,versions,calls=pipeline
    pool=audit.RoundAuditPool(directory,processes=False)
    folder=directory/'collected/host-37/port-1'
    pool.submit(folder);pool.submit(folder)
    result=pool.finish()
    pool.verify_evidence()
    assert sorted(calls)==[1,2] and result['totals']['effective_decisions']==7
    versions[1]='changed'
    with pytest.raises(ValueError,match='before learning'):pool.verify_evidence()


def test_failed_match_poisoned_round_never_passes(pipeline,monkeypatch):
    directory,_,_=pipeline
    def fail(*args):raise ValueError('native receipt mismatch')
    monkeypatch.setattr(audit,'audit_match',fail)
    pool=audit.RoundAuditPool(directory,processes=False)
    with pytest.raises(ValueError,match='native receipt'):pool.finish()
    assert pool.closed
    with pytest.raises(ValueError,match='complete audited round'):pool.verify_evidence()


def test_round_and_path_binding(pipeline):
    directory,_,_=pipeline
    pool=audit.RoundAuditPool(directory,processes=False)
    with pytest.raises(ValueError,match='outside frozen'):pool.submit(directory/'other/host-37/port-1')
    pool.submit(directory/'collected/host-37/port-1')
    (directory/'match-plan.json').write_text('{}')
    with pytest.raises(ValueError,match='plan changed'):pool.finish()
    pool.close()
