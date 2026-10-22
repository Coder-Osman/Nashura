"""Actual-play scheduling, FAIR tracking, reproducible sampling and touch races."""
import copy
from dataclasses import replace
import json
import math
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
import torch

from native_runner.live.adapter import PartialLiveAdapter
from native_runner.live.events import PublicEventCursor, event_watermark
from native_runner.live.policy import PartialLivePolicy
from native_runner.live.scheduler import LiveScheduler
from native_runner.live.snapshot import (CaptureReadError, LiveStateError,
                                         same_battle_continuation)
from native_runner.live.touch import deploy_once, TouchCancelled, TouchTransaction, TouchEnded
from native_runner.live.tracker import LivePublicTracker
from native_runner.training import event_checkpoint
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import EventModelConfigV1
from native_runner.training.tracking import AvailabilityIndex
from native_runner.training.v4.config import scaled_model_config_v4


@pytest.fixture
def row():
    value = json.loads((Path(__file__).parent/'fixtures/live-chef-towers.json').read_text())
    value.update(tick=300, received_monotonic_ns=time.monotonic_ns())
    value['public_events'] = dict(schema='cr-live-public-events.v1', first_sequence=1,
                                 next_sequence=1, started_tick=150, complete=True, events=[])
    return value


def play(row, *, tick, card=26000000, owner=None, cost=3, form=0):
    row['tick'] = tick
    stream = row['public_events']
    event = dict(sequence=stream['next_sequence'], tick=tick,
                 owner=1-row['own_side'] if owner is None else owner,
                 card_id=card, cost=cost, form_code=form, source='native-consume-card.v1',
                 card_identity='own-deck' if owner == row['own_side'] else 'consumed-descriptor')
    stream['events'].append(event)
    stream['next_sequence'] += 1
    return event


def plan(tick=300, delay_ms=5000):
    return dict(tick=tick, act=True, delay_ms=delay_ms)


@pytest.mark.parametrize('delay_ms, click_tick, policy_tick', [
    (100, 300, 302), (600, 300, 312), (999, 300, 320), (1000, 300, 320),
    (1001, 301, 321), (1100, 302, 322), (1600, 312, 332), (5000, 380, 400),
])
def test_compensated_click_deadline_preserves_sampled_delay(delay_ms, click_tick, policy_tick):
    scheduler = LiveScheduler()
    decision = plan(delay_ms=delay_ms)
    scheduler.decide(decision)
    assert decision['delay_ms'] == delay_ms
    assert decision['policy_scheduled_tick'] == policy_tick
    assert decision['input_lead_ms'] == 1000
    assert decision['scheduled_delay_ms'] == (click_tick-300)*50
    assert scheduler.take_due(click_tick-1) is None
    assert scheduler.take_due(click_tick) is decision
    assert scheduler.take_due(click_tick+1) is None


def test_zero_compensation_restores_previous_deadline():
    scheduler = LiveScheduler(input_lead_ms=0)
    decision = plan(delay_ms=100)
    scheduler.decide(decision)
    assert scheduler.take_due(301) is None
    assert scheduler.take_due(302) is decision
    assert decision['scheduled_tick'] == decision['policy_scheduled_tick'] == 302


@pytest.mark.parametrize('lead', [-50, 1, 999, 1000.0, True])
def test_invalid_input_lead_is_rejected(lead):
    with pytest.raises(ValueError, match='nonnegative multiple of 50'):
        LiveScheduler(input_lead_ms=lead)


def test_new_actual_play_replaces_five_second_plan_at_three_seconds(row):
    cursor, scheduler = PublicEventCursor(), LiveScheduler()
    scheduler.observe(row, cursor.update(row))
    original = plan()
    scheduler.decide(original)
    play(row, tick=360)
    assert scheduler.observe(row, cursor.update(row)) is original
    assert scheduler.take_due(400) is None
    replacement = plan(360, 1000)
    scheduler.decide(replacement)
    assert scheduler.take_due(359) is None
    assert scheduler.take_due(360) is replacement
    assert scheduler.take_due(400) is None


def test_bats_skeletons_and_other_spawn_changes_do_not_cancel(row):
    cursor, scheduler = PublicEventCursor(), LiveScheduler()
    scheduler.observe(row, cursor.update(row))
    original = plan()
    scheduler.decide(original)
    for tick, name in ((320, 'Bat'), (340, 'Skeleton'), (360, 'Golemite')):
        row.update(tick=tick, entities=[dict(id=tick, native_data_name=name)])
        assert scheduler.observe(row, cursor.update(row)) is None
        assert scheduler.trigger is None
    assert scheduler.take_due(379) is None
    assert scheduler.take_due(380) is original


def test_spell_without_spawn_cancels_even_if_sample_arrives_after_due(row):
    cursor, scheduler = PublicEventCursor(), LiveScheduler()
    scheduler.observe(row, cursor.update(row))
    original = plan(delay_ms=1600)
    scheduler.decide(original)
    play(row, tick=310, card=28000001, cost=4)
    row['tick'] = 324
    assert scheduler.observe(row, cursor.update(row)) is original
    assert scheduler.take_due(row['tick']) is None


def test_wait_uses_training_timeout_without_extra_full_elixir_resampling(row):
    scheduler = LiveScheduler(max_wait_ticks=300)
    scheduler.decide(dict(tick=300, act=False))
    row['own']['elixir_raw'] = 100000
    row['tick'] = 599
    scheduler.observe(row, ())
    assert scheduler.trigger is None
    row['tick'] = 600
    scheduler.observe(row, ())
    assert scheduler.trigger == 'wait_timeout'


@pytest.mark.parametrize('mutation', [
    lambda r:r.pop('public_events'),
    lambda r:r['public_events'].update(complete=False),
    lambda r:r['public_events'].update(next_sequence=99),
    lambda r:r['public_events']['events'][0].update(tick=999),
    lambda r:r['public_events']['events'][0].update(opponent_hand=[1, 2]),
])
def test_bad_event_stream_never_falls_back_to_spawn_guessing(row, mutation):
    play(row, tick=301)
    mutation(row)
    with pytest.raises(LiveStateError):
        PublicEventCursor().update(row)


def test_events_are_exactly_once_immutable_and_bound_to_one_battle(row):
    cursor = PublicEventCursor()
    play(row, tick=301)
    assert len(cursor.update(row)) == 1
    assert cursor.update(row) == ()
    altered = copy.deepcopy(row)
    altered['public_events']['events'][0]['cost'] = 2
    with pytest.raises(LiveStateError, match='changed'):
        cursor.update(altered)
    row['battle_epoch'] += 1
    with pytest.raises(LiveStateError, match='another battle'):
        cursor.update(row)


def test_tracker_learns_only_played_cards_and_public_costs(row):
    adapter = PartialLiveAdapter()
    tracker = LivePublicTracker(owner=row['own_side'], card_specs=adapter.bundle.card_specs,
                                timeline=adapter.timeline)
    enemy = 1-row['own_side']
    tracker.consume(row)
    assert tracker.elixir_interval(enemy) == (0, 10)
    assert tracker.revealed_cards(enemy) == ()
    play(row, tick=320, card=26000000, cost=3)
    tracker.consume(row)
    assert tracker.elixir_interval(enemy) == (0, 7)
    assert tracker.card_state(enemy, 26000000).cycle_distance == 4
    for offset, card in enumerate((26000001, 26000002, 26000003, 26000004), 1):
        cost = int(adapter.bundle.card_specs[card].elixir_cost)
        play(row, tick=320+offset*200, card=card, cost=cost)
        tracker.consume(row)
    assert tracker.card_state(enemy, 26000000).availability == AvailabilityIndex.AVAILABLE
    assert tracker.card_state(enemy, 28000000).availability == AvailabilityIndex.UNKNOWN_INITIAL
    assert tracker.consume(row) == ()
    observation = adapter.observation(row)
    assert observation.players[enemy].elixir_exact is None
    assert observation.players[enemy].hand == () and observation.players[enemy].deck == ()


def test_ambiguous_mirror_and_special_elixir_keep_uncertainty(row):
    adapter = PartialLiveAdapter()
    tracker = LivePublicTracker(owner=row['own_side'], card_specs=adapter.bundle.card_specs,
                                timeline=adapter.timeline)
    enemy = 1-row['own_side']
    play(row, tick=320, cost=3)
    tracker.consume(row)
    play(row, tick=340, cost=4)  # Mirror's effective card, not another root Knight.
    tracker.consume(row)
    assert tracker.card_state(enemy, 26000000).availability == AvailabilityIndex.UNKNOWN_INITIAL
    play(row, tick=600, card=27000007, cost=6)
    tracker.consume(row)
    assert tracker.elixir_interval(enemy) == (0, 10)


def test_destroyed_towers_remain_in_model_and_update_crowns_and_placement(row):
    adapter = PartialLiveAdapter()
    before = adapter.observation(row)
    enemy = 1-row['own_side']
    tower = next(t for t in before.towers if t.owner == enemy and t.tower_kind == 'princess_left')
    row['entities'] = [e for e in row['entities'] if e['id'] != tower.entity_id]
    row['tick'] += 1
    after = adapter.observation(row)
    assert len(after.towers) == 6
    destroyed = next(t for t in after.towers if t.entity_id == tower.entity_id)
    assert destroyed.hitpoints == 0 and not destroyed.active
    assert after.players[row['own_side']].crowns == 1
    assert after.action_mask.placement_masks != before.action_mask.placement_masks
    fork = adapter.fork()
    assert fork.tower_cache is not adapter.tower_cache


def test_actual_live_timer_frame_uses_training_entity_identity(row):
    # Exact fields first captured by the new reader, including tower targets.
    row['entities'][0]['phase'] = dict(attack_timeline_ms=0, load_remaining_ms=100,
        attack_sequence_stage=0, deploy_remaining_ms=0, deploy_previous_ms=0,
        configured_deploy_time_ms=0, hit_speed_ms=1000, attack_dash_time_ms=0,
        movement_validated=False, target_id=row['entities'][-1]['id'], target_validated=True,
        hooks_attested=True, events=[])
    observation = PartialLiveAdapter().observation(row)
    tower = observation.towers[0]
    assert tower.attack_state.cooldown_remaining_ms == 100
    assert tower.visible_target == row['entities'][-1]['id']


def test_recorded_combat_phase_facts_pass_full_model_contract(checkpoint):
    fixture = json.loads((Path(__file__).parent/'fixtures/live-combat-phases.json').read_text())
    policy = PartialLivePolicy(checkpoint, seed=0)
    policy.observe(fixture['opening'])
    row = fixture['combat']
    result = policy.infer(row)
    observation = policy.adapter.observation(row)
    assert result['event_watermark'] == len(row['public_events']['events'])
    assert observation.entities
    assert any(e.attack_state.target_entity is not None for e in observation.entities)
    assert all(e.attack_state.phase.value == 'unknown' for e in observation.entities)
    assert any(e.movement_runtime['effective_speed'] is not None for e in observation.entities)
    assert all(e.runtime_provenance.field_evidence['attack_state'].value == 'native_derived'
               for e in observation.entities if e.attack_state is not None)


def test_recorded_hero_tombstone_and_charge_overshoot_pass_model(checkpoint):
    fixture = json.loads((Path(__file__).parent/'fixtures/live-hero-tombstone.json').read_text())
    policy = PartialLivePolicy(checkpoint, seed=0)
    policy.observe(fixture['opening'])
    checked_helpers = 0
    checked_charge = False
    for row in fixture['frames']:
        result = policy.infer(row)
        assert math.isfinite(result['value']) and math.isfinite(result['p_act'])
        observation = policy.adapter.observation(row)
        frame = policy.tensorizer.tensorize(observation)
        for entity in observation.entities:
            if entity.native_data_global_id in (2573438603, 3587309677):
                vocab = policy.adapter.bundle.entity_archetype_catalog.runtime_global_vocab_id(entity.native_data_global_id)
                selected = frame.groups.child_archetype_id[0] == vocab
                assert selected.any()  # A helper is encoded, not dropped or changed to UNKNOWN.
                dynamic = frame.groups.child_features[0][selected]
                assert dynamic[0, 1].item() == pytest.approx(entity.hitpoints / 10000)
                assert dynamic[0, 38:40].tolist() == [0.0, 1.0]  # damage immunity known
                checked_helpers += 1
            movement = entity.movement_runtime
            if movement and (movement.get('classic_charge_progress') or 0) > 10000:
                assert movement['classic_charge_phase'] == 'ready'
                checked_charge = True
    assert checked_helpers and checked_charge


def test_unresolved_target_is_unknown_not_a_verified_empty_target(row):
    from native_runner.live.runtime import entity_runtime
    entity = row['entities'][0]
    entity['phase'] = dict(attack_timeline_ms=0, load_remaining_ms=0,
        attack_sequence_stage=0, deploy_remaining_ms=0, deploy_previous_ms=0,
        configured_deploy_time_ms=0, hit_speed_ms=1000, attack_dash_time_ms=0,
        movement_validated=False, target_id=None, target_validated=False,
        hooks_attested=False, events=[])
    runtime = entity_runtime(row, entity)
    assert 'visible_target' not in runtime
    assert runtime['attack_state'].provenance.field_evidence['target_entity'].value == 'unknown'
    assert runtime['attack_state'].phase.value == 'unknown'


@pytest.fixture(scope='module')
def checkpoint(tmp_path_factory):
    model = event_checkpoint._build_model(
        replace(scaled_model_config_v4(.125), snapshot_only=True),
        EventModelConfigV1(delay_hidden=32, cell_hidden=16), EventPolicyConfigV1())
    with torch.no_grad():
        for parameter in model.gate_head.parameters():
            parameter.zero_()
        model.gate_head[-1].bias.copy_(torch.tensor([math.log(.6), math.log(.4)]))
    path = tmp_path_factory.mktemp('live-model')/'policy.pt'
    event_checkpoint.save_event_checkpoint(path, model)
    return path


@pytest.fixture
def evolution_capture():
    return json.loads((Path(__file__).parent/'fixtures/live-evolution-ready.json').read_text())


def test_recorded_evolution_readiness_out_of_hand_reaches_model(checkpoint, evolution_capture):
    policy = PartialLivePolicy(checkpoint, seed=0)
    policy.observe(evolution_capture['opening'])
    card_id = 26000047
    for key in ('before', 'ready', 'blocked'):
        row = evolution_capture[key]
        result = policy.infer(row)
        assert math.isfinite(result['value'])
        observation = policy.adapter.observation(row)
        frame = policy.tensorizer.tensorize(observation)
        vocab = policy.adapter.bundle.card_catalog.vocab_id(card_id)
        index = frame.own_cards.card_vocab_id[0].tolist().index(vocab)
        expected = 0 if key == 'before' else 1
        assert frame.own_cards.runtime_form[0, index].item() == expected
        assert frame.own_cards.runtime_features[0, index, 3:6].tolist() == [float(expected), float(expected), 0.0]
        if expected:
            assert card_id not in observation.players[row['own_side']].hand
            state, = observation.players[row['own_side']].evolution_runtime_states
            assert state.ready is True and state.active is None and state.current_form_id is None
            assert state.provenance.field_evidence['ready'].value == 'native_derived'
            assert state.provenance.field_evidence['deployments_in_cycle'].value == 'unknown'
    assert all(e.evolution_state is None for e in observation.entities)


def test_evolution_in_hand_candidate_and_plan_keep_exact_form(checkpoint, evolution_capture):
    policy = PartialLivePolicy(checkpoint, seed=0)
    row = evolution_capture['blocked']
    policy.observe(evolution_capture['opening'])
    card = row['own']['deck'][0]
    row['own']['hand'][0]['card'] = card
    row['own']['elixir_raw'] = 100000
    # One legal card makes sampled execution exercise the evolved candidate.
    for entry in row['own']['hand'][1:]:
        selected = entry['card']
        selected['cost'] = row['own']['deck'][selected['deck_slot']]['cost'] = 15
    decision = policy.infer(row)
    assert decision['act'] and decision['card_id'] == 26000047
    assert decision['form_code'] == 1 and decision['effective_cost'] == 7
    observation = policy.adapter.observation(row)
    frame = policy.tensorizer.tensorize(observation)
    assert frame.candidates.mask.sum() == 1
    assert frame.candidates.effective_form[frame.candidates.mask].tolist() == [1]
    row['received_monotonic_ns'] = time.monotonic_ns()
    from native_runner.live.run import plan_is_current
    assert plan_is_current(row, decision, policy)
    row['own']['deck'][0]['form_code'] = card['form_code'] = 0
    assert not plan_is_current(row, decision, policy)
    # A fresh stateless observation must not retain readiness from an earlier frame.
    observation = policy.adapter.observation(row)
    assert not observation.players[row['own_side']].evolution_runtime_states
    frame = policy.tensorizer.tensorize(observation)
    assert frame.candidates.effective_form[frame.candidates.mask].tolist() == [0]


def test_evolution_changes_during_selection_cancel_before_target(checkpoint, evolution_capture, tmp_path, monkeypatch):
    from native_runner.live.run import plan_is_current
    policy = PartialLivePolicy(checkpoint, seed=0)
    row = evolution_capture['blocked']
    card = row['own']['deck'][0]
    row['own']['hand'][0]['card'] = card
    row['own']['elixir_raw'] = 100000
    observation = policy.adapter.observation(row)
    rows = observation.action_mask.placement_masks['0']['row_major']
    cell = next((x,y) for y, values in enumerate(rows) for x,value in enumerate(values) if value)
    decision = dict(battle_epoch=row['battle_epoch'], own_side=row['own_side'], slot=0,
                    card_id=card['card_id'], form_code=1, effective_cost=7, native_cell=cell)
    taps = []

    def tap(x, y):
        taps.append((x,y))
        row['own']['deck'][0]['form_code'] = card['form_code'] = 0
        return 20

    kwargs = fake_touch(row, tmp_path, monkeypatch, tap)
    transaction = TouchTransaction()

    def guard(latest):
        if not plan_is_current(latest, decision, policy):
            raise TouchCancelled('form changed')

    with pytest.raises(TouchCancelled, match='form changed'):
        deploy_once(**kwargs, transaction=transaction, guard=guard)
    assert taps == [(332,1700)] and not transaction.committed


@pytest.mark.parametrize('receipt_form,receipt_cost,confirmed', [(1,7,True), (0,7,False), (1,8,False)])
def test_evolution_receipt_confirms_selected_form_and_cost(evolution_capture, tmp_path, monkeypatch,
                                                         receipt_form, receipt_cost, confirmed):
    row = evolution_capture['blocked']
    card = row['own']['deck'][0]
    row['own']['hand'][0]['card'] = card
    taps = []

    def tap(x,y):
        taps.append((x,y))
        if len(taps) == 2:
            play(row, tick=row['tick']+1, owner=row['own_side'], card=card['card_id'],
                 cost=receipt_cost, form=receipt_form)
        return 20

    kwargs = fake_touch(row, tmp_path, monkeypatch, tap)
    transaction = TouchTransaction()
    if confirmed:
        result = deploy_once(**kwargs, transaction=transaction)
        assert result['native_receipt']['form_code'] == 1
    else:
        with pytest.raises(LiveStateError, match='differs from selected form or cost'):
            deploy_once(**kwargs, transaction=transaction)
        assert 'unconfirmed' in kwargs['receipt'].read_text()
    assert len(taps) == 2 and transaction.committed


def test_sampling_is_reproducible_and_does_not_turn_forty_percent_into_never(row, checkpoint):
    first = PartialLivePolicy(checkpoint, seed=0)
    second = PartialLivePolicy(checkpoint, seed=0)
    outputs = []
    for step in range(8):
        row['tick'] = 300+step
        row['sequence'] += 1
        a, b = first.infer(row), second.infer(row)
        assert {k:v for k,v in a.items() if k != 'inference_ms'} == {k:v for k,v in b.items() if k != 'inference_ms'}
        assert a['p_act'] == pytest.approx(.4)
        assert a['temperatures'] == [1., 1., 1., 1.]
        assert a['act'] == (a['sample_uniforms'][0] >= .6)
        outputs.append(a['act'])
    assert any(outputs) and not all(outputs)


def test_future_affordability_is_enabled_with_only_payable_delay_bins(row, checkpoint, monkeypatch):
    policy = PartialLivePolicy(checkpoint, seed=0)
    golem = next(c for c in row['own']['deck'] if c['card_id'] == 26000009)
    row['own']['hand'][0]['card'] = golem
    # Keep the hand unique if the captured hand already contained Golem.
    chosen = [golem] + [c for c in row['own']['deck'] if c['card_id'] != golem['card_id']][:3]
    row['own']['hand'] = [dict(slot=i, card=c) for i,c in enumerate(chosen)]
    row['own']['elixir_raw'] = 70000
    original = policy.model.select_action
    found = {}
    from native_runner.live import policy as module
    original_ids = module.row_card_ids

    def identities(*args, **kwargs):
        result = original_ids(*args, **kwargs)
        found['ids'] = result.tolist()
        return result

    def select(context, **kwargs):
        index = found['ids'].index(26000009)
        assert kwargs['card_mask'][0, index]
        assert not kwargs['delay_mask'][0, index, :29].any()
        assert kwargs['delay_mask'][0, index, 29:].any()
        found['checked'] = True
        return original(context, **kwargs)

    monkeypatch.setattr(module, 'row_card_ids', identities)
    monkeypatch.setattr(policy.model, 'select_action', select)
    policy.infer(row)
    assert found['checked']


def fake_touch(row, tmp_path, monkeypatch, on_tap):
    capture, receipt = tmp_path/'capture.jsonl', tmp_path/'receipt.jsonl'
    capture.write_text(json.dumps(dict(serial='test-device', pid=123))+'\n')
    row['received_monotonic_ns'] = time.monotonic_ns()
    session = SimpleNamespace(serial='test-device', check_capture=lambda path:None,
                              transport=SimpleNamespace(tap=on_tap))
    monkeypatch.setattr('native_runner.live.touch.latest_snapshot', lambda path:row)
    card = row['own']['hand'][0]['card']
    row['own']['elixir_raw'] = 100000
    return dict(adb=Path('unused'), serial='test-device', capture=capture, receipt=receipt,
                epoch=row['battle_epoch'], slot=0, card_id=card['card_id'], screen=(540, 1000), session=session)


def test_play_arriving_during_card_selection_prevents_target_touch(row, tmp_path, monkeypatch):
    taps = []

    def tap(x, y):
        taps.append((x, y))
        play(row, tick=301, card=28000001, cost=4)
        return 20

    kwargs = fake_touch(row, tmp_path, monkeypatch, tap)
    transaction = TouchTransaction()

    def guard(latest):
        if event_watermark(latest):
            raise TouchCancelled('new play')

    with pytest.raises(TouchCancelled):
        deploy_once(**kwargs, transaction=transaction, guard=guard)
    assert taps == [(332, 1700)] and not transaction.committed
    assert 'cancelled_before_target' in kwargs['receipt'].read_text()


def test_committed_touch_confirms_from_own_native_receipt_even_before_hand_rotation(row, tmp_path, monkeypatch):
    taps = []
    card = row['own']['hand'][0]['card']

    def tap(x, y):
        taps.append((x, y))
        if len(taps) == 2:
            play(row, tick=301, owner=row['own_side'], card=card['card_id'], cost=card['cost'])
        return 20

    kwargs = fake_touch(row, tmp_path, monkeypatch, tap)
    transaction = TouchTransaction()
    result = deploy_once(**kwargs, transaction=transaction)
    assert result['native_receipt']['card_id'] == card['card_id']
    assert result['hand_rotated'] is False
    assert transaction.committed and not transaction.cancel('too late')
    assert len(taps) == 2


def test_committed_touch_confirms_after_capture_epoch_rebind(row, tmp_path, monkeypatch):
    taps = []
    card = row['own']['hand'][0]['card']
    rebound = copy.deepcopy(row)
    rebound['battle_epoch'] += 1
    rebound['tick'] += 1
    rebound['public_events'] = dict(schema='cr-live-public-events.v1', first_sequence=1,
                                    next_sequence=1, started_tick=row['tick'], complete=True,
                                    events=[])
    play(rebound, tick=rebound['tick'], owner=rebound['own_side'], card=card['card_id'],
         cost=card['cost'])
    states = iter((row, rebound))
    monkeypatch.setattr('native_runner.live.touch.latest_snapshot', lambda path: next(states))
    kwargs = fake_touch(row, tmp_path, monkeypatch, lambda x, y: taps.append((x, y)) or 20)
    rebound['received_monotonic_ns'] = time.monotonic_ns()
    # fake_touch's first snapshot is consumed by its setup-independent call;
    # restore the iterator so the touch sees the pre- and post-rebind states.
    states = iter((row, row, row, row, rebound))
    monkeypatch.setattr('native_runner.live.touch.latest_snapshot', lambda path: next(states))
    result = deploy_once(**kwargs, transaction=TouchTransaction())
    assert result['event'] == 'native_play_confirmed'
    assert result['epoch_rebound'] is True
    assert result['after_epoch'] == rebound['battle_epoch']
    assert len(taps) == 2


@pytest.mark.parametrize('outcome', ['ended', 'ended_with_receipt', 'another_battle'])
def test_touch_terminal_keeps_native_receipt_and_battle_identity(row, tmp_path, monkeypatch, outcome):
    taps = []
    card = row['own']['hand'][0]['card']

    def tap(x, y):
        taps.append((x, y))
        if len(taps) == 2:
            row.update(ended=True, winner=row['own_side'], tick=row['tick']+1)
            if outcome == 'ended_with_receipt':
                play(row, tick=row['tick'], owner=row['own_side'], card=card['card_id'], cost=card['cost'])
            if outcome == 'another_battle':
                row['battle_epoch'] += 1
        return 20

    kwargs = fake_touch(row, tmp_path, monkeypatch, tap)
    transaction = TouchTransaction()
    if outcome == 'ended_with_receipt':
        result = deploy_once(**kwargs, transaction=transaction)
        assert result['event'] == 'native_play_confirmed'
    elif outcome == 'ended':
        with pytest.raises(TouchEnded) as error:
            deploy_once(**kwargs, transaction=transaction)
        assert error.value.after['winner'] == row['own_side']
        assert 'unconfirmed_at_terminal' in kwargs['receipt'].read_text()
    else:
        with pytest.raises(LiveStateError, match='battle changed') as error:
            deploy_once(**kwargs, transaction=transaction)
        assert not isinstance(error.value, TouchEnded)
    assert len(taps) == 2 and transaction.committed


@pytest.mark.parametrize('error_at_send', [False, True])
@pytest.mark.parametrize('recovery', ['receipt', 'terminal', 'another_battle', 'script_error',
                                    'persistent_error', 'stale_receipt'])
def test_pending_receipt_survives_only_bounded_observation_errors(row, tmp_path, monkeypatch,
                                                                error_at_send, recovery):
    taps, reads = [], []
    clock = [0.0]
    monkeypatch.setattr('native_runner.live.touch.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('native_runner.live.touch.time.monotonic_ns', lambda: 1_000_000_000 + int(clock[0]*1e9))
    monkeypatch.setattr('native_runner.live.touch.time.sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    kwargs = fake_touch(row, tmp_path, monkeypatch, lambda x, y: taps.append((x, y)) or 20)
    recovered = copy.deepcopy(row)
    recovered['tick'] += 5
    card = recovered['own']['hand'][0]['card']
    play(recovered, tick=recovered['tick'], owner=recovered['own_side'], card=card['card_id'], cost=card['cost'])
    if recovery == 'terminal':
        recovered['public_events'] = copy.deepcopy(row['public_events'])
        recovered.update(ended=True, winner=recovered['own_side'])
    if recovery == 'another_battle':
        recovered['battle_epoch'] += 1
        recovered['tick'] = 100
        recovered['public_events'] = dict(schema='cr-live-public-events.v1', first_sequence=1,
                                          next_sequence=1, started_tick=0, complete=True, events=[])

    def latest(_):
        if len(taps) < 2:
            return row
        reads.append(clock[0])
        if len(reads) == 1 and not error_at_send:
            return row
        if recovery == 'script_error':
            raise LiveStateError('live capture is not ready: script_error')
        if clock[0] < .24 or recovery == 'persistent_error':
            raise CaptureReadError('live capture is not ready: read_error: duplicate entity identity')
        recovered['received_monotonic_ns'] = time.monotonic_ns() - (600_000_000 if recovery == 'stale_receipt' else 0)
        return recovered

    monkeypatch.setattr('native_runner.live.touch.latest_snapshot', latest)
    transaction = TouchTransaction()
    if recovery == 'receipt':
        result = deploy_once(**kwargs, transaction=transaction)
        assert result['event'] == 'native_play_confirmed'
        assert result['native_receipt']['card_id'] == card['card_id']
        assert result['touch_sent_tick'] == (None if error_at_send else row['tick'])
    else:
        expected = {'terminal': 'battle ended', 'another_battle': 'battle changed',
                    'script_error': 'script_error', 'persistent_error': 'did not recover within 3 seconds',
                    'stale_receipt': 'did not recover within 3 seconds'}[recovery]
        with pytest.raises(LiveStateError, match=expected) as error:
            deploy_once(**kwargs, transaction=transaction)
        assert isinstance(error.value, TouchEnded) == (recovery == 'terminal')
    assert len(taps) == 2 and transaction.committed
    assert clock[0] <= 3.04  # Repeated errors do not extend the original deadline.
    events = [json.loads(line)['event'] for line in kwargs['receipt'].read_text().splitlines()]
    assert events.count('target_touch_started') == 1
    assert ('receipt_observation_resumed' in events) == (recovery in ('receipt', 'terminal'))
    assert events.count('receipt_observation_wait') <= 2


def test_read_error_before_target_cancels_without_deployment(row, tmp_path, monkeypatch):
    taps = []
    kwargs = fake_touch(row, tmp_path, monkeypatch, lambda x, y: taps.append((x, y)) or 20)

    def latest(_):
        if taps:
            raise CaptureReadError('discarded sample')
        return row

    monkeypatch.setattr('native_runner.live.touch.latest_snapshot', latest)
    transaction = TouchTransaction()
    with pytest.raises(CaptureReadError):
        deploy_once(**kwargs, transaction=transaction)
    assert len(taps) == 1 and not transaction.committed
