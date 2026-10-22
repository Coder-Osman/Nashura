from collections import Counter, defaultdict
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from native_runner.training.event_opponents import (
    QUOTAS, build_plan, select_pool, freeze_plan, validate_plan, validate_shard_ownership,
    checkpoint_records, match_metadata,
    result_statistics,
)

FLEET = {h: {'ports': list(range(100, 100+s+n))} for h, (s,n) in QUOTAS.items()}
RECORDS = [dict(iteration=i, sha256=f'{i:064x}') for i in (600,645,650,675,700,725,750,775)]


def plan(iteration=790):
    return build_plan(iteration=iteration, seed_base=2180918000, current_sha='a'*64,
                      pool=select_pool(RECORDS, iteration-1, 'a'*64), fleet=FLEET)


def test_quotas_pairing_rolling_pool_and_sha_dedup():
    for iteration, expected in ((690,[675,650]), (710,[675,650]), (711,[700,675,650]), (790,[775,750,725,700])):
        p=plan(iteration)
        assert [r['iteration'] for r in p['pool']['recent']] == expected
        assert Counter(m['kind'] for m in p['matches']) == {'self':72,'fixed':12,'recent':24}
        assert Counter(m['learner_owner'] for m in p['matches']) == {None:72,0:18,1:18}
        assert p['expected_player_lanes'] == 180
        for h,(s,n) in QUOTAS.items():
            games=[m for m in p['matches'] if m['host']==h]
            assert sum(m['kind']=='self' for m in games)==s
            assert sum(m['kind']!='self' for m in games)==n
        pairs=defaultdict(list)
        for m in p['matches']:
            if m['pair_id']:pairs[m['pair_id']].append(m)
        assert len(pairs)==18
        for a,b in pairs.values():
            assert a['seed']==b['seed'] and a['learner_owner']!=b['learner_owner']
            assert a['owner_sha256']==b['owner_sha256'][::-1]
        assert validate_plan(p)==p
    records=deepcopy(RECORDS)
    records[-1]['sha256']=records[-2]['sha256']
    selected=select_pool(records,789,'a'*64)['recent']
    assert [r['iteration'] for r in selected]==[775,725,700,675]


def test_freeze_retry_reuses_plan_and_rejects_changed_identity(tmp_path):
    p=plan()
    kwargs=dict(iteration=790, seed_base=p['seed_base'], current_sha=p['current_sha256'], pool=p['pool'], fleet=FLEET)
    path=tmp_path/'plan.json'
    assert freeze_plan(path,**kwargs)==p
    assert freeze_plan(path,**{**kwargs,'pool':plan(690)['pool']})==p
    with pytest.raises(ValueError,match='identity'):
        freeze_plan(path,**{**kwargs,'current_sha':'b'*64})
    changed=deepcopy(p);changed['matches'][0]['seed']+=1
    with pytest.raises(ValueError,match='SHA'):
        validate_plan(changed)
    assert build_plan(**kwargs)==p


def test_only_complete_records_missing_pool_fails(tmp_path):
    (tmp_path/'iteration-000675').mkdir()
    (tmp_path/'iteration-000650').mkdir()
    (tmp_path/'iteration-000650'/'success.json').write_text(json.dumps(dict(iteration=650,checkpoint='/weights/'+('1'*64)+'.pt')))
    assert [r['iteration'] for r in checkpoint_records(tmp_path)]==[650]
    with pytest.raises(ValueError,match='fixed'):
        select_pool(checkpoint_records(tmp_path),700,'a'*64)
    with pytest.raises(ValueError,match='no eligible'):
        select_pool(RECORDS,650,'a'*64)


def test_draw_margin_is_not_counted_as_a_win():
    p=plan();m=next(m for m in p['matches'] if m['learner_owner']==0)
    counters=[dict(mixed_opponents=match_metadata(p,m),results={'0':r,'1':-r},elapsed_s=2.)
              for r in (.2,-.2,0.,1.1,-.8)]
    group=next(iter(result_statistics(counters).values()))
    assert group==dict(games=5,wins=1,draws=3,losses=1,seconds=10.)


@pytest.mark.parametrize('owner',[0,1])
def test_shard_rejects_historical_rows_even_if_labeled_current(owner):
    p=plan()
    match=next(m for m in p['matches'] if m['learner_owner']==owner)
    meta=match_metadata(p,match)
    row=SimpleNamespace(owner=owner,match_id=match['game_id'],behavior_checkpoint_sha256=p['current_sha256'])
    shard=SimpleNamespace(manifest={'contract':{'mixed_opponents':meta}},records=[row])
    validate_shard_ownership(shard,meta)
    shard.records.append(SimpleNamespace(owner=1-owner,match_id=match['game_id'],behavior_checkpoint_sha256=p['current_sha256']))
    with pytest.raises(ValueError,match='frozen opponent'):
        validate_shard_ownership(shard,meta)
