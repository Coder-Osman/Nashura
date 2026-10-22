"""Meter-to-learner coverage for the 0.003 overflow / beta=1 and beta=0.15 reward contracts."""
import copy
import pytest
import torch

from native_runner.tests.test_event_overflow import timeline, save, _recorder
from native_runner.tests.test_event_tower_rewards import decision, state
from native_runner.training.event_rewards import (
    STRONG_TOWER_OVERFLOW_REWARD_CONFIG as OLD,
    LOW_OVERFLOW_TOWER_REWARD_CONFIG as NEW,
    MODERATE_TOWER_OVERFLOW_REWARD_CONFIG as MODERATE,
    ElixirOverflowMeter, overflow_penalty, reward_config,
)
from native_runner.training.event_preflight import load_complete_rollout, EventPreflightError
from native_runner.training.event_ppo import event_lane_advantages, mc_value_targets
from native_runner.tests.test_recruits_hogs import five_plan
from native_runner.tests.test_mixed_decks import FLEET
from native_runner.training.mixed_decks import build_plan
from native_runner.training.event_opponents import freeze_plan
from tools.event_rl_round_audit import audit_match
from native_runner.training.event_shard_cache import VerifiedShardCache


def collected(config, folder):
    folder.mkdir()
    recorder=_recorder();recorder.reward_config=config
    meter=ElixirOverflowMeter(timeline(),config)
    for owner in (0,1):meter.start(owner,100)
    full={0:100000,1:100000}
    for tick,hp,cancelled in [(100,1000.,False),(110,900.,True),(120,800.,False)]:
        decision(recorder,tick,state(enemy=hp),act=cancelled,cancelled=cancelled)
        penalty=meter.advance(start=tick,stop=tick+10,before=full,after=full)
        recorder.advance(ticks=10,overflow_penalties=penalty,
                         rewards={0:1.1} if tick==120 else {},done=tick==120)
    path=save(recorder,folder/'part-0.json')
    (folder/'collector.log').write_text('{}\n')
    return path,meter.report()


def test_cost_and_contract_identity():
    for raw in (0,10000,100000,5000000):
        assert overflow_penalty(raw,NEW)==pytest.approx(raw/10000*.003)
        assert overflow_penalty(raw,NEW)==pytest.approx(.3*overflow_penalty(raw,OLD))
    assert NEW['penalty_cap'] is None and OLD['penalty_per_elixir']==.01
    assert NEW['tower_potential']==OLD['tower_potential']
    wrong=copy.deepcopy(OLD);wrong['penalty_per_elixir']=.003
    with pytest.raises(ValueError):reward_config(wrong)
    wrong=copy.deepcopy(NEW);wrong['tower_potential']['coefficient']=.1
    with pytest.raises(ValueError):reward_config(wrong)


def test_meter_serialization_audit_gae_and_cancelled_credit(tmp_path):
    lanes=[];counts=[]
    for name,config in [('old',OLD),('new',NEW)]:
        path,report=collected(config,tmp_path/name)
        lane,count=load_complete_rollout([path]);lanes.append(lane[0]);counts.append(count)
        assert count['reward_config']==config
        assert count['trainable_rows']==2 and count['excluded_rows']==1
        for owner in ('0','1'):
            assert report['owners'][owner]['penalty']==pytest.approx(30*178/10000*config['penalty_per_elixir'])
        result=audit_match(path.parent,dict(kind='recent',learner_owner=0,game_id='test'),
                           dict(reward_config=config),VerifiedShardCache())
        assert result['totals']['effective_decisions']==2
        assert result['totals']['cancelled_decisions']==1
    a,b=(x['reward_components'] for x in counts)
    assert b['overflow_penalty']==pytest.approx(.3*a['overflow_penalty'])
    assert b['effective_overflow_penalty']==pytest.approx(.3*a['effective_overflow_penalty'])
    assert b['tower_shaping']==pytest.approx(a['tower_shaping'])
    assert b['training_reward']-a['training_reward']==pytest.approx(.7*a['overflow_penalty'])
    assert lanes[0].outcome_reward==lanes[1].outcome_reward==1.1
    mc=mc_value_targets(lanes[1],gamma=.999)
    _,returns=event_lane_advantages(lanes[1],torch.zeros(2),gamma=.999,gae_lambda=1.)
    torch.testing.assert_close(returns,mc)
    with pytest.raises(EventPreflightError):
        load_complete_rollout([tmp_path/'old/part-0.json',tmp_path/'new/part-0.json'])


def test_round_plan_freezes_new_reward_and_rejects_reuse(tmp_path):
    p=five_plan()
    args=dict(builder=build_plan,iteration=p['iteration'],seed_base=p['seed_base'],current_sha=p['current_sha256'],
              pool=p['pool'],fleet=FLEET,start_iteration=p['start_iteration'],action_constraints=p['action_constraints'],
              deck_policy=p['deck_policy'])
    old=freeze_plan(tmp_path/'old.json',**args,reward_config=OLD)
    new=freeze_plan(tmp_path/'new.json',**args,reward_config=NEW)
    assert old['matches']==new['matches'] and old['sha256']!=new['sha256']
    assert new['reward_config']==NEW
    with pytest.raises(ValueError):freeze_plan(tmp_path/'old.json',**args,reward_config=NEW)


def test_beta015_changes_only_tower_shaping_and_reaches_auditor(tmp_path):
    parts=[]
    for name,config in [('beta1',NEW),('beta015',MODERATE)]:
        path,_=collected(config,tmp_path/name)
        lanes,counts=load_complete_rollout([path])
        assert counts['excluded_rows']==1 and counts['trainable_rows']==2
        result=audit_match(path.parent,dict(kind='recent',learner_owner=0,game_id=name),
                          dict(reward_config=config),VerifiedShardCache())
        assert result['components']==pytest.approx(counts['reward_components'])
        parts.append(counts['reward_components'])
    a,b=parts
    assert b['base_reward']==a['base_reward']
    assert b['overflow_penalty']==a['overflow_penalty']
    for key in ['tower_shaping','tower_change_reward','tower_discount_reward','tower_terminal_settlement']:
        assert b[key]==pytest.approx(.15*a[key])
    assert b['training_reward']==pytest.approx(b['base_reward']-b['overflow_penalty']+b['tower_shaping'])
    bad=copy.deepcopy(NEW);bad['tower_potential']['coefficient']=.15
    with pytest.raises(ValueError):reward_config(bad)
    with pytest.raises(EventPreflightError):
        load_complete_rollout([tmp_path/'beta1/part-0.json',tmp_path/'beta015/part-0.json'])
