from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from native_runner.timeline import BattleTimelineV1, ElixirRateV1, TimelineSectionV1
from native_runner.training.event_rewards import (
    ElixirOverflowMeter, OVERFLOW_REWARD_CONFIG as CONFIG, rate_segments, raw_elixir,
)
from native_runner.training.event_rollout import lanes_from_shard
from native_runner.training.event_cache import load_event_rl_shard
from native_runner.training.event_preflight import load_complete_rollout, EventPreflightError
from native_runner.training.event_ppo import mc_value_targets, event_lane_advantages, event_ppo_update, EventPpoConfigV1
from native_runner.training.event_batches import row_batches, PreparedEventRollout
from native_runner.tests.test_event_rollout import _recorder, _decision
from native_runner.tests.test_event_batches import fixture_lanes, clone_model, RecordingAdamW
from native_runner.tests.test_recruits_hogs import five_plan
from native_runner.tests.test_mixed_decks import FLEET
from native_runner.training.event_opponents import freeze_plan, match_metadata, validate_shard_ownership
from native_runner.training.mixed_decks import build_plan, validate_plan


def timeline():
    return BattleTimelineV1("fixture", 5., (TimelineSectionV1(300, "normal"),),
        (ElixirRateV1(120, 28000, 1), ElixirRateV1(120, 14000, 2), ElixirRateV1(60, 9300, 3)), "", "")


def meter():
    m = ElixirOverflowMeter(timeline(), CONFIG)
    for o in (0, 1):
        m.start(o, 0)
    return m


def receipt(cost, tick, identity="a", owner=0, kind="action_executed"):
    return SimpleNamespace(event_type=kind, owner=owner, tick=tick,
                           data=dict(action_id=identity, effective_cost=cost))


@pytest.mark.parametrize("before,after,waste", [(50000, 51780, 0), (99500, 100000, 1280), (100000, 100000, 1780)])
def test_only_actual_regeneration_loss_is_penalized(before, after, waste):
    m = meter()
    result = m.advance(start=0, stop=10, before={0:before,1:before}, after={0:after,1:after})
    assert result == {0:waste/10000*.02, 1:waste/10000*.02}


def test_integer_rates_boundaries_and_partition_invariance():
    t = timeline()
    segments = list(rate_segments(t, 2390, 4810))
    assert [r for _, _, r, _ in segments] == [178, 357, 537]
    reports = []
    for stops in ([5000], [10, 2390, 2400, 2410, 4800, 5000], list(range(1,5001))):
        m = meter(); start=0; bar=50000
        for stop in stops:
            generated = sum((b-a)*r for a,b,r,_ in rate_segments(t,start,stop))
            after=min(100000,bar+generated)
            m.advance(start=start,stop=stop,before={0:bar,1:bar},after={0:after,1:after})
            bar,start=after,stop
        reports.append(m.report())
    for report in reports:
        assert report['owners']['0']['waste_raw'] == reports[0]['owners']['0']['waste_raw']
        assert report['owners']['0']['penalty'] == .5
        assert report['owners']['1']['penalty'] == .5
        for key in ('1','2','3'):
            a=report['owners']['0']['by_multiplier'][key];b=reports[0]['owners']['0']['by_multiplier'][key]
            assert a['waste_raw'] == b['waste_raw']
            assert a['penalty'] == pytest.approx(b['penalty'])


def test_confirmed_cost_only_and_receipt_deduplication():
    m = meter()
    a=receipt(3,1)
    result=m.advance(start=0,stop=10,before={0:50000,1:50000},after={0:21780,1:51780},
        outcomes=[a,a,receipt(3,1,'cancel',kind='action_rejected')])
    assert result == {0:0.,1:0.}
    m.advance(start=10,stop=20,before={0:21780,1:51780},after={0:23560,1:53560},outcomes=[a])
    assert m.report()['receipt_count']==1
    assert m.report()['owners']['0']['single_elixir_plays']==1
    with pytest.raises(ValueError,match='conflicting'):
        m.advance(start=20,stop=21,before={0:1,1:1},after={0:1,1:1},outcomes=[receipt(4,1)])


@pytest.mark.parametrize('execution_tick', [2399,2400,2401,2402])
def test_spend_before_native_regeneration_at_rate_boundary(execution_tick):
    reports=[]
    for boundaries in ([2390,2410],list(range(2390,2411))):
        m=meter();bar=100000
        for start,stop in zip(boundaries,boundaries[1:]):
            before=bar
            for tick in range(start,stop):
                if tick==execution_tick-1:bar-=30000
                rate=next(rate_segments(timeline(),tick,tick+1))[2]
                bar=min(100000,bar+rate)
            events=[receipt(3,execution_tick)] if start<execution_tick<=stop else []
            m.advance(start=start,stop=stop,before={0:before,1:100000},after={0:bar,1:100000},outcomes=events)
        reports.append(m.report())
    for phase in ('1','2','3'):
        a,b=(r['owners']['0']['by_multiplier'][phase] for r in reports)
        assert a['waste_raw']==b['waste_raw']
        assert a['penalty']==pytest.approx(b['penalty'])


def test_inconsistent_resource_source_or_rate_is_rejected():
    m=meter()
    with pytest.raises(ValueError,match='conservation'):
        m.advance(start=0,stop=10,before={0:50000,1:50000},after={0:51779,1:51780})
    with pytest.raises(ValueError,match='conservation'):
        m.advance(start=0,stop=10,before={0:50000,1:50000},after={0:51781,1:51780})


def test_initialization_first_decision_terminal_tail_and_independent_caps():
    m=ElixirOverflowMeter(timeline(),CONFIG)
    full={0:100000,1:100000}
    assert m.advance(start=0,stop=30,before=full,after=full)=={0:0.,1:0.}
    m.start(0,30)
    m.advance(start=30,stop=2030,before=full,after=full)
    m.start(1,2030)
    m.advance(start=2030,stop=2040,before=full,after=full,gameplay_after=False)
    m.advance(start=2040,stop=2060,before=full,after=full,gameplay_before=False,gameplay_after=False)
    a,b=m.report()['owners'].values()
    assert a['penalty']==.5 and b['penalty']==0.
    assert a['waste_raw']==356000
    assert a['skipped_terminal_ticks']==b['skipped_terminal_ticks']==10
    assert a['measured_ticks']==2000 and b['measured_ticks']==0


def save(recorder,path):
    recorder.to_shard(path.with_suffix('.crev.zst'),shard_id=path.stem,contract={})
    return path


def test_filtered_prefix_and_tail_conserve_components_and_true_draw(tmp_path):
    r=_recorder();r.reward_config=CONFIG
    _decision(r,0,100,executed=False)
    r.advance(ticks=10,overflow_penalties={0:.1})
    _decision(r,0,110,act=False,log_prob=-.4)
    r.advance(ticks=10,overflow_penalties={0:.1})
    _decision(r,0,120,executed=False)
    r.advance(ticks=10,rewards={0:0.},overflow_penalties={0:.1},done=True)
    lanes,counts=load_complete_rollout([save(r,tmp_path/'new.json')])
    lane=lanes[0]
    assert lane.time_steps==1 and lane.outcome_reward==0.
    assert lane.reward.tolist()==pytest.approx([-.2])
    assert r.results()[0]==0.
    components=counts['reward_components']
    assert components['overflow_penalty']==pytest.approx(.3)
    assert components['prefix_overflow_penalty']==pytest.approx(.1)
    assert components['effective_overflow_penalty']==pytest.approx(.2)
    assert next(row_batches(lanes,chunk_steps=10)).episode_reward.tolist()==[0.]
    assert counts['excluded_rows']==2 and counts['trainable_rows']==1
    assert counts['reward_config']==CONFIG


def test_legacy_reading_and_mixed_configuration_rejected(tmp_path):
    r=_recorder();_decision(r,0,100);r.advance(ticks=10,rewards={0:1.},done=True)
    old=save(r,tmp_path/'old.json')
    assert load_complete_rollout([old])[0][0].outcome_reward==1.
    r=_recorder();r.reward_config=CONFIG
    _decision(r,0,100);r.advance(ticks=10,rewards={0:1.},overflow_penalties={0:.02},done=True)
    new=save(r,tmp_path/'new.json')
    assert load_event_rl_shard(new).records[0].base_reward==1.
    with pytest.raises(EventPreflightError,match='reward configuration mismatch'):
        load_complete_rollout([old,new])


def test_complete_mc_returns_and_gae():
    _,lanes=fixture_lanes(); lane=replace(lanes[1],reward=torch.tensor([-.1,-.2,.7]),terminal_reward=1.)
    expected=torch.tensor([-.1+.9*(-.2+.9*.7),-.2+.9*.7,.7])
    torch.testing.assert_close(mc_value_targets(lane,gamma=.9),expected)
    advantage,returns=event_lane_advantages(lane,torch.zeros(3),gamma=.9,gae_lambda=1.)
    torch.testing.assert_close(returns,expected);torch.testing.assert_close(advantage,expected)


def test_packed_and_ordinary_ppo_use_same_shaped_rewards_and_raw_outcomes():
    original,lanes=fixture_lanes()
    lanes=[replace(l,reward=l.reward-.03,terminal_reward=float(l.reward[-1])) for l in lanes]
    results=[]
    for packed in (False,True):
        model=clone_model(original);optimizer=RecordingAdamW(model.parameters(),lr=1e-5)
        cache=PreparedEventRollout(model,lanes,row_batch_size=4) if packed else None
        stats=event_ppo_update(model,lanes,optimizer,prepared=cache,reference_model=original,
            config=EventPpoConfigV1(minibatch_lanes=0,sequence_chunk_steps=2,epochs=1,bc_coefficient=.3,kl_coefficient=.1))
        results.append((optimizer.gradients,stats))
    for a,b in zip(results[0][0],results[1][0]):
        if a is None: assert b is None
        else: torch.testing.assert_close(a,b,atol=3e-5,rtol=3e-4)
    for key in ('value_loss','policy_loss','bc_loss','bc_kept_fraction'):
        assert getattr(results[0][1],key)==pytest.approx(getattr(results[1][1],key),abs=2e-5)


def test_freeze_reward_contract_without_changing_match_allocation(tmp_path):
    p=five_plan()
    args=dict(builder=build_plan,iteration=p['iteration'],seed_base=p['seed_base'],current_sha=p['current_sha256'],
        pool=p['pool'],fleet=FLEET,start_iteration=p['start_iteration'],action_constraints=p['action_constraints'],
        deck_policy=p['deck_policy'],reward_config=CONFIG)
    frozen=freeze_plan(tmp_path/'plan.json',**args)
    assert validate_plan(frozen)['matches']==p['matches']
    assert frozen['sha256']!=p['sha256']
    assert freeze_plan(tmp_path/'plan.json',**args)==frozen
    with pytest.raises(ValueError,match='curriculum'):
        freeze_plan(tmp_path/'plan.json',**dict(args,reward_config=None))
    metadata=match_metadata(frozen,frozen['matches'][0])
    assert metadata['reward_config']==CONFIG
    with pytest.raises(ValueError,match='reward configuration'):
        validate_shard_ownership(SimpleNamespace(manifest={'contract':{}}),metadata)
