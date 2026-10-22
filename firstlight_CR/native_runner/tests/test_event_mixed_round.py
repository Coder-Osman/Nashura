import json
from pathlib import Path

import pytest

from native_runner.tests.test_event_opponents import plan
from native_runner.tests.test_event_rollout import _recorder, _decision
from native_runner.training.event_opponents import match_metadata
from native_runner.training.event_preflight import load_complete_rollout, EventPreflightError
from tools.event_rl_round_validation import validate_round, ERROR_COUNTERS


def test_complete_mixed_round_learns_180_current_lanes_and_checks_both_receipts(tmp_path):
    p=plan()
    (tmp_path/'match-plan.json').write_text(json.dumps(p))
    cache=tmp_path/'collected'
    summaries=[]
    for match in p['matches']:
        directory=cache/f"host-{match['host']}"/f"port-{match['port']}"
        directory.mkdir(parents=True)
        recorder=_recorder()
        recorder.match_id=match['game_id']
        recorder.behavior_checkpoint_sha256=p['current_sha256']
        recorder.behavior_model_contract_hash='contract'
        owners=[0,1] if match['kind']=='self' else [match['learner_owner']]
        for owner in owners:
            _decision(recorder,owner,100,executed=False)
        recorder.advance(ticks=10)
        for owner in owners:
            _decision(recorder,owner,110)
        recorder.advance(ticks=10,rewards={0:1.,1:-1.},done=True)
        meta=match_metadata(p,match)
        recorder.to_shard(directory/'part-00.crev.zst',shard_id='part-00',contract={'mixed_opponents':meta})
        summary=directory/'part-00.json';summaries.append(summary)
        counter=dict.fromkeys(ERROR_COUNTERS,0)
        counter.update(scheduler_version='event-scheduling.v3',terminal=True,
            source_checkpoint_sha256=p['current_sha256'],mixed_opponents=meta,recorded_owners=owners,
            submitted=2,confirmed=2,executed=2,confirmed_by_owner={'0':1,'1':1},cancelled_native_terminal=0,
            rows=2*len(owners), recovery=0,cancelled_cell=0,elapsed_s=10.,results={'0':1.,'1':-1.})
        (directory/'collector.log').write_text(json.dumps(counter)+'\n')
    actual=validate_round(cache,108,p['current_sha256'])
    assert actual['mixed_opponents']['player_lanes']==180
    assert actual['confirmed']==216 and actual['trainable_rows']==180 and actual['excluded_rows']==180
    rows=[]
    lanes,counts=load_complete_rollout(summaries,expected_checkpoint_sha256=p['current_sha256'],
        expected_model_contract_hash='contract',row_metadata=rows,match_plan=p)
    assert len(lanes)==counts['training_lanes']==180
    assert counts['rows']==360 and counts['trainable_rows']==180
    assert all(lane.time_steps==1 for lane in lanes)
    assert all(row['behavior_checkpoint_sha256']==p['current_sha256'] for lane in rows for row in lane)
    assert sum(lane.reward.item()>0 for lane in lanes)==90
    assert counts['mixed_opponents']['plan_sha256']==p['sha256']
    with pytest.raises(EventPreflightError,match='requires its frozen'):
        load_complete_rollout(summaries)
    with pytest.raises(EventPreflightError,match='missing matches'):
        load_complete_rollout(summaries[:-1],expected_checkpoint_sha256=p['current_sha256'],match_plan=p)
    history=next(m for m in p['matches'] if m['kind']!='self')
    log=cache/f"host-{history['host']}"/f"port-{history['port']}"/'collector.log'
    corrupted=json.loads(log.read_text());corrupted['rejected']=1;corrupted['submitted']+=1
    log.write_text(json.dumps(corrupted))
    with pytest.raises(ValueError,match='invalid scheduled'):
        validate_round(cache,108,p['current_sha256'])
