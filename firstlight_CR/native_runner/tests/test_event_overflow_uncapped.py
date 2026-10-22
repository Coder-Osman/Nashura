"""Versioned no-cap rewards must survive metering, cancellation and PPO loading."""
import math
from dataclasses import replace

import pytest

from native_runner.tests.test_event_overflow import timeline, save, _recorder, _decision
from native_runner.tests.test_recruits_hogs import five_plan
from native_runner.tests.test_mixed_decks import FLEET
from native_runner.training.event_rewards import (
    ElixirOverflowMeter, OVERFLOW_REWARD_CONFIG as V1,
    UNCAPPED_OVERFLOW_REWARD_CONFIG as V2, overflow_penalty, reward_config,
)
from native_runner.training.event_preflight import load_complete_rollout, EventPreflightError
from native_runner.training.event_opponents import freeze_plan
from native_runner.training.mixed_decks import build_plan
from native_runner.training.event_cache import load_event_rl_shard
from native_runner.training.event_ppo import event_lane_advantages


@pytest.mark.parametrize('raw,old,new', [(0,0,0),(10000,.02,.01),(250000,.5,.25),
    (500000,.5,.5),(1000000,.5,1.),(2000000,.5,2.)])
def test_exact_linear_cost_and_legacy_cap(raw, old, new):
    assert overflow_penalty(raw,V1)==old
    assert overflow_penalty(raw,V2)==new


@pytest.mark.parametrize('config', [V1,V2])
def test_phase_and_interval_partition_invariance(config):
    reports=[]
    for boundaries in ([0,6000],[0,1405,2400,4800,6000],list(range(6001))):
        m=ElixirOverflowMeter(timeline(),config)
        for o in (0,1):m.start(o,0)
        full={0:100000,1:100000}
        for start,stop in zip(boundaries,boundaries[1:]):
            m.advance(start=start,stop=stop,before=full,after=full)
        reports.append(m.report())
    expected_raw=2400*178+2400*357+1200*537
    for report in reports:
        for data in report['owners'].values():
            assert data['waste_raw']==expected_raw
            assert data['penalty']==pytest.approx(overflow_penalty(expected_raw,config))
            assert data['cap_reached']==(config==V1)
            assert sum(p['penalty'] for p in data['by_multiplier'].values())==pytest.approx(data['penalty'])
            for phase in ('1','2','3'):
                assert data['by_multiplier'][phase]['penalty']==pytest.approx(
                    reports[0]['owners']['0']['by_multiplier'][phase]['penalty'])
    if config==V2:
        assert reports[0]['owners']['0']['by_multiplier']['3']['penalty']>0


def test_uncapped_cost_above_terminal_win_reaches_gae_and_cancellation_filter(tmp_path):
    r=_recorder();r.reward_config=V2
    _decision(r,0,100,executed=False)
    r.advance(ticks=10,overflow_penalties={0:.1})
    _decision(r,0,110,act=False)
    r.advance(ticks=10,overflow_penalties={0:.6})
    _decision(r,0,120,executed=False)
    r.advance(ticks=10,overflow_penalties={0:.6},rewards={0:1.},done=True)
    path=save(r,tmp_path/'uncapped.json')
    lanes,counts=load_complete_rollout([path])
    assert counts['reward_config']==V2
    assert counts['excluded_rows']==2 and counts['trainable_rows']==1
    assert lanes[0].outcome_reward==1. and r.results()[0]==1.
    assert lanes[0].reward.tolist()==pytest.approx([-.2])
    _,returns=event_lane_advantages(lanes[0],lanes[0].reward*0,gamma=.999,gae_lambda=.95)
    assert returns.tolist()==pytest.approx([-.2])
    assert counts['reward_components']['effective_overflow_penalty']==pytest.approx(1.2)
    assert counts['reward_components']['prefix_overflow_penalty']==pytest.approx(.1)
    record=load_event_rl_shard(path).records[0]
    for bad in (float('nan'),float('inf'),-.1):
        with pytest.raises(ValueError):replace(record,overflow_penalty=bad)


def test_legacy_excess_and_mixed_versions_are_rejected(tmp_path):
    paths=[]
    for name,config,cost in [('v1',V1,.02),('v2',V2,.01),('bad',V1,.6)]:
        r=_recorder();r.reward_config=config
        _decision(r,0,100,act=False)
        r.advance(ticks=10,overflow_penalties={0:cost},rewards={0:1.},done=True)
        paths.append(save(r,tmp_path/(name+'.json')))
    with pytest.raises(EventPreflightError,match='configuration mismatch'):
        load_complete_rollout(paths[:2])
    with pytest.raises(EventPreflightError,match='exceeds match cap'):
        load_complete_rollout(paths[2:])


def test_version_and_frozen_plan_identity(tmp_path):
    for invalid in (dict(V1,penalty_cap=None),dict(V2,penalty_per_elixir=.02),dict(V2,penalty_cap=math.inf)):
        with pytest.raises(ValueError):reward_config(invalid)
    copied=reward_config(V2);copied['penalty_cap']=0
    assert V2['penalty_cap'] is None
    p=five_plan()
    args=dict(builder=build_plan,iteration=p['iteration'],seed_base=p['seed_base'],current_sha=p['current_sha256'],
        pool=p['pool'],fleet=FLEET,start_iteration=p['start_iteration'],action_constraints=p['action_constraints'],
        deck_policy=p['deck_policy'])
    old=freeze_plan(tmp_path/'old.json',**args,reward_config=V1)
    new=freeze_plan(tmp_path/'new.json',**args,reward_config=V2)
    assert old['matches']==new['matches']==p['matches']
    assert old['sha256']!=new['sha256']
    assert freeze_plan(tmp_path/'old.json',**args,reward_config=V1)==old
    with pytest.raises(ValueError,match='curriculum'):
        freeze_plan(tmp_path/'old.json',**args,reward_config=V2)
