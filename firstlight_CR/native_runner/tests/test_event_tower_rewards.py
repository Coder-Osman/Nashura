"""Potential credit must follow effective decisions and preserve terminal scores."""
from dataclasses import replace
import copy

import pytest
import torch

from native_runner.training.event_tower_rewards import (
    tower_potential, validate_tower_state, validate_reward_gamma, TOWER_POTENTIAL,
)
from native_runner.training.event_rewards import (
    TOWER_OVERFLOW_REWARD_CONFIG as V3, UNCAPPED_OVERFLOW_REWARD_CONFIG as V2,
    OVERFLOW_REWARD_CONFIG as V1, reward_config, reward_components,
)
from native_runner.training.event_rollout import training_segments, lanes_from_shard
from native_runner.training.event_cache import load_event_rl_shard
from native_runner.training.event_preflight import load_complete_rollout, EventPreflightError
from native_runner.training.event_ppo import mc_value_targets, event_lane_advantages
from native_runner.tests.test_event_rollout import _recorder, _decision
from native_runner.tests.test_event_overflow import save


def state(*, enemy=1000., own=1000., enemy_king=1000., own_king=1000.):
    return tuple((o*3+i, o, kind, (own_king if o==0 else enemy_king) if i==0
                  else (own if o==0 else enemy), 1000.) for o in (0,1)
                 for i,kind in enumerate(('king','princess_left','princess_right')))


def decision(recorder, tick, evidence, *, owner=0, act=True, cancelled=False):
    _decision(recorder, owner, tick, act=act, executed=not cancelled)
    recorder._pending[owner].tower_state=evidence
    if cancelled:
        recorder.cancel(owner, reason='test-interruption')


def fixture(*, cancelled=True, config=V3):
    r=_recorder();r.reward_config=config
    decision(r,100,state(enemy=900.))
    r.advance(ticks=10,overflow_penalties={0:.01})
    if cancelled:
        decision(r,110,state(enemy=700.),cancelled=True)
        r.advance(ticks=10,overflow_penalties={0:.02})
        decision(r,120,state(enemy=600.),cancelled=True)
        r.advance(ticks=10,overflow_penalties={0:.03})
    else:
        r.advance(ticks=20,overflow_penalties={0:.05})
    decision(r,130,state(enemy=500.),act=False)
    r.advance(ticks=10,overflow_penalties={0:.04},rewards={0:1.1},done=True)
    return r


def test_normalization_symmetry_and_destroyed_towers():
    assert tower_potential(state(),0)==0.
    assert tower_potential(state(enemy=0.,enemy_king=0.),0)==1.
    assert tower_potential(state(own=0.,own_king=0.),0)==-1.
    for s in (state(enemy=700.),state(own=0.,enemy_king=300.)):
        assert tower_potential(s,0)==-tower_potential(s,1)
    assert tower_potential(state(enemy=0.),0)==.4
    assert tower_potential(state(enemy_king=0.),0)==.6


@pytest.mark.parametrize('bad', [None,(),state()[:-1],state()[:-1]+state()[:1],
    state(enemy=-1.),state(enemy=1001.),state(enemy=float('nan')),state(enemy=float('inf')),
    ((0,0,'king',0.,0.),)+state()[1:]])
def test_invalid_evidence_rejected(bad):
    with pytest.raises(ValueError):validate_tower_state(bad)


def test_cancelled_decisions_do_not_change_credit_and_terminal_settles():
    r=fixture();clean=fixture(cancelled=False)
    actual=training_segments(r.rows[0],reward_config=V3)
    expected=training_segments(clean.rows[0],reward_config=V3)
    assert [i for i,_ in actual]==[0,3]
    assert [x.reward for _,x in actual]==pytest.approx([x.reward for _,x in expected])
    assert [x.decision_ticks for _,x in actual]==[30,10]
    assert [x.execution_status for _,x in actual]==['executed','wait']
    p0,p1=tower_potential(state(enemy=900.),0),tower_potential(state(enemy=500.),0)
    assert actual[0][1].tower_shaping==pytest.approx(.1*(.999*p1-p0))
    assert actual[1][1].tower_shaping==pytest.approx(-.1*p1)
    assert sum(.999**i*x.tower_shaping for i,(_,x) in enumerate(actual))==pytest.approx(-.1*p0)
    assert all(x.tower_shaping==0 for x in r.rows[0])
    assert r.results()[0]==1.1


def test_cancelled_prefix_and_tail_and_empty_lane():
    r=_recorder();r.reward_config=V3
    decision(r,100,state(enemy=900.),cancelled=True)
    r.advance(ticks=10,overflow_penalties={0:.1})
    decision(r,110,state(enemy=700.),act=False)
    r.advance(ticks=10,overflow_penalties={0:.2})
    decision(r,120,state(enemy=100.),cancelled=True)
    r.advance(ticks=10,rewards={0:1.},overflow_penalties={0:.3},done=True)
    rows=training_segments(r.rows[0],reward_config=V3)
    assert len(rows)==1 and rows[0][1].done
    assert rows[0][1].tower_shaping==pytest.approx(-.1*tower_potential(state(enemy=700.),0))
    assert rows[0][1].overflow_penalty==pytest.approx(.5)
    parts=reward_components(r.rows[0],V3)
    assert parts['prefix_overflow_penalty']==pytest.approx(.1)
    assert parts['effective_training_reward']==pytest.approx(rows[0][1].reward)
    empty=_recorder();empty.reward_config=V3
    decision(empty,100,state(),cancelled=True);empty.advance(ticks=10,rewards={0:1.},done=True)
    assert training_segments(empty.rows[0],reward_config=V3)==[]
    assert reward_components(empty.rows[0],V3)['tower_shaping']==0


def test_recorded_owner_only_and_independent_clocks(tmp_path):
    r=fixture()
    decision(r,105,state(enemy=900.),owner=1,act=False)
    r.advance(ticks=40,rewards={1:-1.1},done=True)
    path=tmp_path/'owner.json'
    r.to_shard(path.with_suffix('.crev.zst'),shard_id='owner',contract={},owner=0)
    shard=load_event_rl_shard(path,verify=True)
    assert {x.owner for x in shard.records}=={0}
    lanes,counts=load_complete_rollout([path])
    assert len(lanes)==1 and counts['training_lanes']==1
    assert lanes[0].outcome_reward==1.1


def test_real_serialization_gae_mc_and_component_conservation(tmp_path):
    r=fixture();path=save(r,tmp_path/'tower.json')
    shard=load_event_rl_shard(path,verify=True)
    assert shard.records[0].tower_state==r.rows[0][0].tower_state
    lanes,counts=load_complete_rollout([path]);lane=lanes[0]
    expected=[row.reward for _,row in training_segments(r.rows[0],reward_config=V3)]
    assert lane.reward.tolist()==pytest.approx(expected)
    assert lane.outcome_reward==1.1 and counts['excluded_rows']==2
    parts=counts['reward_components']
    assert parts['training_reward']==pytest.approx(parts['base_reward']-parts['overflow_penalty']+parts['tower_shaping'])
    assert parts['tower_shaping']==pytest.approx(parts['tower_change_reward']+parts['tower_discount_reward']+parts['tower_terminal_settlement'])
    assert parts['tower_changed_transitions']==1
    assert parts['tower_telescoping_error_max']<1e-8
    mc=mc_value_targets(lane,gamma=.999)
    _,gae=event_lane_advantages(lane,torch.zeros(lane.time_steps),gamma=.999,gae_lambda=1.)
    torch.testing.assert_close(mc,gae)
    for action in (lambda:mc_value_targets(lane,gamma=.99),
                   lambda:event_lane_advantages(lane,torch.zeros(lane.time_steps),gamma=.99,gae_lambda=.95)):
        with pytest.raises(ValueError,match='gamma'):action()


def test_discount_only_is_not_reported_as_tower_change():
    r=fixture(cancelled=False)
    r.rows[0][1]=replace(r.rows[0][1],tower_state=r.rows[0][0].tower_state)
    c=reward_components(r.rows[0],V3)
    assert c['tower_changed_transitions']==0 and c['tower_change_reward']==0.
    assert c['tower_discount_reward']!=0.


def test_evidence_mismatch_double_shaping_truncation_rejected():
    rows=fixture().rows[0]
    for bad in ([replace(rows[0],tower_state=None),*rows[1:]],
                [*rows[:-1],replace(rows[-1],tower_state=tuple((*t[:4],2000.) for t in rows[-1].tower_state))],
                [*rows[:-1],replace(rows[-1],done=False,truncated=True)]):
        with pytest.raises(ValueError):training_segments(bad,reward_config=V3)
    derived=[r for _,r in training_segments(rows,reward_config=V3)]
    with pytest.raises(ValueError,match='twice'):training_segments(derived,reward_config=V3)


def test_old_versions_unchanged_and_mixing_rejected(tmp_path):
    for cfg in (V1,V2,None):
        if cfg is None:
            r=_recorder();_decision(r,0,100);r.advance(ticks=10,rewards={0:1.},done=True)
        else:r=fixture(config=cfg)
        path=save(r,tmp_path/(str(cfg and cfg['version'])+'.json'))
        lane=load_complete_rollout([path])[0][0]
        assert lane.reward.sum().item()==pytest.approx(1. if cfg is None else 1.)
        assert 'tower_shaping' not in reward_components(r.rows[0],cfg)
        with pytest.raises(EventPreflightError,match='configuration mismatch'):
            load_complete_rollout([path,save(fixture(),tmp_path/'v3.json')])
    invalid=copy.deepcopy(V3);invalid['tower_potential']['gamma']=.99
    with pytest.raises(ValueError):reward_config(invalid)
    validate_reward_gamma(V2,.9)
    with pytest.raises(ValueError):validate_reward_gamma(V3,.9)
