"""Timing joins must not confuse host/device clocks or count deployment as input."""
from pathlib import Path
from types import SimpleNamespace
import io
import json
import threading
import shutil
import subprocess

import pytest

from native_runner.live.input import PersistentInput
from native_runner.live.latency import analyze, device_on_host
from native_runner.live.snapshot import LiveStateError


def write(path, rows):
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))


@pytest.fixture
def timed_job(tmp_path):
    # Guest time and host time intentionally differ by a large offset.
    sync = dict(host_minus_device_min_ns=10_000_000_000,
                host_minus_device_max_ns=10_000_200_000)
    timing = dict(schema='cr-live-decision-timing.v1', decision_id='1:100',
                  decision_finished_monotonic_ns=11_000_000_000,
                  dispatch_monotonic_ns=11_070_000_000,
                  source_tick=100, scheduled_tick=102, selected_delay_ms=100,
                  due_observed_tick=102)
    card = dict(card_id=27000009, deck_slot=2)
    request = dict(event='requested', epoch=1, card_id=27000009, slot=0,
                   decision_timing=timing,
                   before=dict(entities=[], own=dict(hand=[dict(card=card)])))
    def tap(down):
        return dict(clock_sync=sync, device_down_before_ns=down,
                    device_down_after_ns=down+1_000_000,
                    device_up_before_ns=down+21_000_000,
                    device_up_after_ns=down+22_000_000)
    touch = [request, dict(event='hand_touch_completed', input_timing=tap(1_075_000_000)),
             dict(event='target_touch_completed', input_timing=tap(1_100_000_000))]
    write(tmp_path/'policy-touch-001.jsonl', touch)
    write(tmp_path/'policy.jsonl', [dict(event='confirmed', receipt='policy-touch-001.jsonl',
          native_receipt=dict(sequence=3, tick=124))])
    def marker(stage, ns, **fields):
        return dict(event='latency_marker', stage=stage, battle_epoch=1,
                    card_id=27000009, device_monotonic_ns=str(ns), tick=103, **fields)
    markers = [marker('own_command_created', 1_125_000_000, deck_slot=2, command_id=1),
               marker('own_command_submit_returned', 1_126_000_000, command_id=1, accepted=True),
               marker('own_native_consume_enter', 2_175_000_000, public_sequence=3),
               marker('own_native_consumed', 2_176_000_000, public_sequence=3)]
    markers[-2]['tick'] = markers[-1]['tick'] = 124
    markers.append(dict(event='snapshot', battle_epoch=1, tick=125, own_side=0,
        entities=[dict(id=100, owner=0, card_id=27000009,
                       phase=dict(deploy_remaining_ms=950, configured_deploy_time_ms=1000))]))
    markers.append(dict(event='snapshot', battle_epoch=1, tick=143, own_side=0,
        entities=[dict(id=100, owner=0, card_id=27000009, phase=dict(deploy_remaining_ms=50))]))
    markers.append(dict(event='snapshot', battle_epoch=1, tick=145, own_side=0,
        entities=[dict(id=100, owner=0, card_id=27000009, phase=dict(deploy_remaining_ms=0))]))
    write(tmp_path/'capture.jsonl', markers)
    return tmp_path, markers, touch


def test_input_latency_excludes_native_wait_and_deployment(timed_job):
    directory, _markers, _touch = timed_job
    report = analyze(directory)
    action = report['actions'][0]
    assert not report['problems']
    assert action['decision_to_target_down_ms'] == pytest.approx(100.1)
    assert action['decision_to_target_down_bounds_ms'] == [100, 100.2]
    assert action['dispatch_to_target_down_ms'] == pytest.approx(30.1)
    assert action['hand_down_to_target_down_ms'] == 25
    assert action['target_down_to_command_created_ms'] == 25
    assert action['command_created_to_consumption_ms'] == 1050
    assert action['target_down_to_consumption_ms'] == 1075
    assert action['sampled_entity_appearances'][0]['deploy_remaining_ms'] == 950
    assert action['sampled_entity_appearances'][0]['deployment_finished_tick_bounds'] == [143, 145]
    assert action['clock_sync_uncertainty_ms'] == .1
    assert action['input_lead_ms'] == 0
    assert action['policy_scheduled_tick'] == action['scheduled_tick'] == 102
    assert action['scheduled_delay_ms'] == 100


def test_compensated_timing_keeps_policy_and_click_delays_distinct(timed_job):
    directory, _markers, touch = timed_job
    touch[0]['decision_timing'].update(selected_delay_ms=1100, policy_scheduled_tick=122,
                                       input_lead_ms=1000, scheduled_delay_ms=100)
    write(directory/'policy-touch-001.jsonl', touch)
    report = analyze(directory)
    action = report['actions'][0]
    assert not report['problems']
    assert action['selected_delay_ms'] == 1100
    assert action['scheduled_delay_ms'] == 100
    assert action['policy_scheduled_tick'] == 122
    assert action['scheduled_tick'] == 102
    assert action['input_lead_ms'] == 1000
    assert action['target_down_to_consumption_ms'] == 1075


@pytest.mark.parametrize('mutation', ['wrong_card', 'wrong_epoch', 'duplicate', 'rejected'])
def test_bad_command_join_is_reported_not_estimated(timed_job, mutation):
    directory, markers, _touch = timed_job
    if mutation == 'wrong_card':
        markers[0]['card_id'] = 26000000
    elif mutation == 'wrong_epoch':
        markers[0]['battle_epoch'] = 2
    elif mutation == 'duplicate':
        markers.append({**markers[0], 'command_id': 2})
    else:
        markers[1]['accepted'] = False
    write(directory/'capture.jsonl', markers)
    report = analyze(directory)
    assert report['problems']
    assert report['actions'][0]['decision_to_target_down_ms'] > 0
    assert 'command_created_to_consumption_ms' not in report['metrics']


def test_unconfirmed_touch_keeps_click_measurement(timed_job):
    directory, _markers, _touch = timed_job
    write(directory/'policy.jsonl', [])
    report = analyze(directory)
    assert report['actions'][0]['decision_to_target_down_ms'] > 0
    assert report['problems'][0]['reason'] == 'touch has no native confirmation'
    assert 'target_down_to_consumption_ms' not in report['metrics']


def test_terminal_pending_touch_is_recorded_without_inventing_confirmation(timed_job):
    directory, _markers, touch = timed_job
    write(directory/'policy.jsonl', [])
    terminal = dict(battle_epoch=1, tick=110, ended=True, winner=0, own_side=0)
    touch.append(dict(event='unconfirmed_at_terminal', monotonic_ns=14_000_000_000,
                      terminal=terminal))
    write(directory/'policy-touch-001.jsonl', touch)
    report = analyze(directory)
    assert not report['problems']
    assert report['terminal_unconfirmed'][0]['terminal'] == terminal
    assert report['actions'][0]['timing_status'] == 'battle_ended_before_confirmation'
    assert 'command_created_to_consumption_ms' not in report['metrics']


@pytest.mark.parametrize('kind', ['historical_false', 'validation_rejected', 'outbound_stale'])
def test_unconfirmed_diagnostics_do_not_confuse_rejection_queueing_and_stale_capture(timed_job, kind):
    directory, markers, touch = timed_job
    write(directory/'policy.jsonl', [])
    touch.append(dict(event='unconfirmed', monotonic_ns=14_000_000_000))
    write(directory/'policy-touch-001.jsonl', touch)
    markers = markers[:2]
    markers[1]['accepted'] = kind == 'outbound_stale'
    if kind == 'validation_rejected':
        markers[1].update(outcome='validation_rejected', command_destroyed=True, outbound_seen=False,
                          validations=[dict(flags=1, result=13, elixir_raw=50000, reserved_elixir=4)])
    if kind == 'outbound_stale':
        markers.append({**markers[0], 'stage': 'own_command_outbound'})
    markers.append(dict(event='snapshot', battle_epoch=1, tick=110, ended=False,
                        received_monotonic_ns=11_500_000_000))
    # A different battle's return and fresh snapshot must not contaminate this one.
    markers.extend([{**markers[1], 'battle_epoch': 2},
                    {**markers[-1], 'battle_epoch': 2, 'received_monotonic_ns': 13_999_000_000}])
    write(directory/'capture.jsonl', markers)
    diagnostic = analyze(directory)['actions'][0]['unconfirmed_diagnostics']
    assert diagnostic['command_status'] == {
        'historical_false': 'not_accepted_or_locally_queued',
        'validation_rejected': 'validation_rejected',
        'outbound_stale': 'outbound_without_receipt'}[kind]
    assert diagnostic['last_snapshot_age_ms'] == 2500
    assert diagnostic['snapshot_stale'] is True


def test_cancelled_selection_is_not_a_missing_target_error(timed_job):
    directory, _markers, touch = timed_job
    write(directory/'policy-touch-001.jsonl', touch[:2] + [
        dict(event='cancelled_before_target', reason='new public card play')])
    report = analyze(directory)
    assert not report['actions'] and not report['problems']
    assert len(report['cancelled_before_target']) == 1


@pytest.mark.parametrize('case', ['stopped', 'queued', 'outbound', 'freed_unknown', 'cleared', 'foreign'])
def test_sync_fault_diagnosis_requires_destroyed_unsent_command(timed_job, case):
    directory, markers, _touch = timed_job
    write(directory/'policy.jsonl', [])
    markers = markers[:2]
    markers[0]['submit_state'] = dict(battle_stopped=1, defer_enabled=0, queued_commands=0)
    markers[1].update(accepted=False, command_destroyed=True, outbound_seen=False, validations=[])
    fault = dict(event='latency_marker', stage='battle_sync_fault', battle_epoch=1,
                 device_monotonic_ns='1100000000', tick=102,
                 cause='checksum_mismatch_during_step', local_checksum=123, received_checksum=456,
                 battle_sync=dict(stopped=1))
    markers.append(fault)
    if case == 'queued':
        markers[0]['submit_state']['defer_enabled'] = 1
        markers[1]['command_destroyed'] = False
    if case == 'outbound':
        markers.append({**markers[0], 'stage': 'own_command_outbound'})
    if case == 'freed_unknown':
        del markers[0]['submit_state']['battle_stopped']
    if case == 'cleared':
        markers.append(dict(event='latency_marker', stage='battle_sync_state', battle_epoch=1,
            device_monotonic_ns='1110000000', battle_sync=dict(stopped=0)))
    if case == 'foreign':
        fault['battle_epoch'] = 2
    write(directory/'capture.jsonl', markers)
    report = analyze(directory)
    diagnostic = report['actions'][0]['unconfirmed_diagnostics']
    assert (diagnostic['command_status'] == 'battle_sync_stopped') == (case in ('stopped', 'cleared', 'foreign'))
    assert ('sync_fault' in diagnostic) == (case == 'stopped')
    if case == 'stopped':
        assert diagnostic['sync_fault']['local_checksum'] == 123
        assert diagnostic['sync_fault']['received_checksum'] == 456
    assert report['sync_faults'] == [fault]


def test_diagnostic_command_marker_never_replaces_public_observation(tmp_path):
    from native_runner.live.snapshot import latest_snapshot
    from native_runner.live.events import PublicEventCursor
    snapshot = json.loads((Path(__file__).parent/'fixtures/live-chef-towers.json').read_text())
    snapshot['public_events'] = dict(schema='cr-live-public-events.v1', first_sequence=1,
        next_sequence=1, started_tick=0, complete=True, events=[])
    path = tmp_path/'capture.jsonl'
    write(path, [snapshot, dict(event='latency_marker', stage='own_command_created', card_id=27000009)])
    observed = latest_snapshot(path)
    assert observed == snapshot
    assert PublicEventCursor().update(observed) == ()


def test_device_to_host_conversion_preserves_uncertainty():
    assert device_on_host('12345', dict(host_minus_device_min_ns=-12000,
                                      host_minus_device_max_ns=-11900)) == (345, 445)


def transport(answer):
    obj = PersistentInput.__new__(PersistentInput)
    obj.lock = threading.Lock()
    obj.sequence = 5
    obj.identity = 'test'
    obj.clock_sync = dict(host_minus_device_min_ns=0, host_minus_device_max_ns=100)
    obj.process = SimpleNamespace(stdin=io.BytesIO())
    obj._line = lambda timeout: answer
    return obj


def test_input_ack_records_android_down_up_and_transport_bounds():
    obj = transport('OK 6 22 1000000 2000000 22000000 23000000')
    assert obj.tap(540, 1000) == 22
    timing = obj.last_timing
    assert timing['request_sequence'] == 6
    assert timing['device_up_after_ns'] == 23000000
    assert timing['host_sent_ns'] <= timing['host_acknowledged_ns']
    assert obj.process.stdin.getvalue() == b'6 540 1000\n'


@pytest.mark.parametrize('answer', ['OK 5 22 1 2 3 4', 'OK 6 22 4 3 2 1', 'OK 6 22'])
def test_wrong_or_legacy_touch_ack_is_rejected(answer):
    with pytest.raises(LiveStateError):
        transport(answer).tap(540, 1000)


def test_frida_clock_preserves_nanoseconds_without_int64_multiply():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed for the isolated Frida clock fixture')
    source = (Path(__file__).parents[1]/'live/timing-reader.js').read_text()
    # Frida Int64 has no mul(). Keep the fixture limited to its real methods.
    prelude = '''
const Memory = {alloc: () => ({readS64: () => ({toString: () => '12345678'}),
  add: () => ({readS64: () => ({toNumber: () => 123456789})})})};
const Module = {getGlobalExportByName: () => 1};
function NativeFunction() { return () => 0; }
'''
    script = prelude + source + "\nconsole.log(new LiveTimingCapture({}, null).now());"
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == '12345678123456789'


def test_local_command_diagnostics_distinguish_rejection_from_queued():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed for the Frida command fixture')
    tests = Path(__file__).parent
    subprocess.run([node, str(tests / 'live_command_diagnostics.cjs'),
                    str(tests.parent / 'live/timing-reader.js')], check=True, timeout=20)
