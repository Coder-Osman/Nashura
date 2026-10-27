from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e2_1n_concurrency import backend_failed_attempt, backend_rejection, backend_timeout, cross_run_identities, evaluate, original_timeline, race_result, serial_dispatch


def timeline_fixture():
    events = []
    for iteration in range(4):
        for operation in ['write', 'read']:
            first = len(events) // 2 + 1
            for thread, call, overlaps in [(11, first, []), (29, first + 1, [first])]:
                events.append(dict(edge='enter', sequence=len(events) + 1, call=call,
                    thread=thread, operation=operation, wall_milliseconds=1,
                    phase='explicit_post_trajectory_pn_race', caller='external-probe', overlaps=overlaps))
            for thread, call in [(29, first + 1), (11, first)]:
                events.append(dict(edge='leave', sequence=len(events) + 1, call=call,
                    thread=thread, operation=operation, wall_milliseconds=1,
                    phase='explicit_post_trajectory_pn_race', completion='observed caller return instruction'))
    return dict(schema='e2.1n-original-pn-timeline.v4', armed=True, errors=[], dropped=0,
        active=[], pending_threads=0, retained_listeners=2, retained_return_sites=2,
        module_observer_retained=True, general_thread_safety='unknown', no_vm_verified=False,
        strict_gate='BLOCKED', calls=16, events=events,
        coverage='PN native entry and observed caller return instruction only; Java monitors and scheduler ordering unknown')


def test_timeline_requires_actual_matching_returns_and_keeps_unknowns():
    source = timeline_fixture()
    result = original_timeline(source, {11, 29})
    assert result['cross_thread_entry_overlaps'] == 8
    for mutate in [lambda row: row.update(schema='e2.1n-original-pn-timeline.v1'),
                   lambda row: row.update(dropped=1),
                   lambda row: row.update(general_thread_safety='verified'),
                   lambda row: row['events'][-1].update(thread=47),
                   lambda row: row['events'][-1].update(completion='inferred'),
                   lambda row: row['events'][1].update(overlaps=[]),
                   lambda row: row['events'].pop()]:
        changed = deepcopy(source)
        mutate(changed)
        with pytest.raises((ValueError, KeyError)):
            original_timeline(changed, {11, 29})


def rejection_fixture():
    owner = dict(call=1, operation='read', store=0, thread=11, stage='entered')
    jni = dict(owner, stage='jni:native-get')
    rows = [dict(edge='call-enter', thread=11, operation='read', store=0, owner=owner),
            dict(edge='stage-enter', thread=11, stage='jni:native-get', owner=jni),
            dict(edge='rejected', thread=29, operation='read', store=0, owner=jni,
                 busy=True, retired=False, reason='bounded serial PN backend required'),
            dict(edge='stage-return', thread=11, stage='jni:native-get', owner=jni),
            dict(edge='call-return', thread=11, owner=owner)]
    events = [dict(event='pn_backend_timeline', detail=dict(row, sequence=index))
              for index, row in enumerate(rows, 1)]
    events.append(dict(event='pn_bridge_contract_violation', error='explicit fixture'))
    return [dict(payload=dict(type='send', payload=row)) for row in events]


def test_uncovered_return_sites_remain_partial_and_worker_coverage_is_required():
    source = timeline_fixture()
    error = dict(error='Error: PN caller return instruction outside bounded executable coverage',
        operation='read', thread=47, caller='external-probe', completion='unknown', executable_range=None,
        return_address='0x1000', phase='fixture')
    source['errors'] = [error]
    result = original_timeline(source, {11, 29})
    assert result['return_coverage'] == 'partial'
    assert result['uncovered_calls'][0]['completion'] == 'unknown'
    for key, value in [('thread', 11), ('error', 'unexpected'), ('completion', 'inferred'),
                       ('executable_range', 'r-x')]:
        changed = deepcopy(source)
        changed['errors'][0][key] = value
        with pytest.raises(ValueError):
            original_timeline(changed, {11, 29})


def test_rejection_requires_owner_stage_thread_and_independent_bridge_failure():
    source = rejection_fixture()
    assert backend_rejection(source)['rejections'] == [dict(stage='jni:native-get', operation='read', store=0)]
    for mutate in [lambda rows: rows.pop(),
                   lambda rows: rows[2]['payload']['payload']['detail'].update(thread=11),
                   lambda rows: rows[2]['payload']['payload']['detail']['owner'].update(stage='unknown'),
                   lambda rows: rows[4]['payload']['payload']['detail'].update(thread=29)]:
        changed = deepcopy(source)
        mutate(changed)
        with pytest.raises((ValueError, KeyError)):
            backend_rejection(changed)


def test_race_keeps_unknown_reads_and_detach_failures_visible():
    source = dict(status=0, general_thread_safety='unknown', no_vm_verified=False,
        workers=[dict(index=index, thread=11 + index, completed=4, error=0, detached=True,
                      unknown_reads=0, own_reads=3, other_reads=1) for index in range(2)])
    assert race_result(source)[1]['reads'] == 8
    for key, value in [('unknown_reads', 1), ('detached', False), ('completed', 3), ('thread', 11)]:
        changed = deepcopy(source)
        changed['workers'][1][key] = value
        with pytest.raises(ValueError):
            race_result(changed)


def test_timeout_requires_closed_prefix_and_does_not_invent_rejection_or_cause():
    source = rejection_fixture()
    source.pop()
    source.pop(2)
    for sequence, row in enumerate(source, 1):
        row['payload']['payload']['detail']['sequence'] = sequence
    result = backend_timeout(source)
    assert result['entered_calls'] == 1
    assert result['rejections'] == []
    assert result['timeout_cause'] == 'unknown'
    assert result['saved_store_restore'] == 'uncovered after timeout'
    assert result['replacement_concurrency_equivalence'] == 'unverified'
    for mutate in [lambda rows: rows.pop(),
                   lambda rows: rows[1]['payload']['payload']['detail'].update(thread=29),
                   lambda rows: rows[2]['payload']['payload']['detail'].update(stage='unknown'),
                   lambda rows: rows.append(rejection_fixture()[-1])]:
        changed = deepcopy(source)
        mutate(changed)
        with pytest.raises((ValueError, KeyError)):
            backend_timeout(changed)
    with pytest.raises(ValueError):
        backend_rejection(source)
    with pytest.raises(ValueError):
        backend_timeout(rejection_fixture())


def test_rejection_and_timeout_are_independent_observations():
    source = rejection_fixture()
    result = backend_failed_attempt(source)
    assert result['replacement_concurrency_equivalence'] == 'failed for observed attempt'
    assert result['bridge_contract_violations'] == 1
    assert result['termination'] == 'external experiment timeout'
    assert result['timeout_cause'] == 'unknown'
    assert result['saved_store_restore'] == 'uncovered after timeout'
    source.pop()
    with pytest.raises(ValueError):
        backend_failed_attempt(source)
    source.pop(2)
    for sequence, row in enumerate(source, 1):
        row['payload']['payload']['detail']['sequence'] = sequence
    result = backend_failed_attempt(source)
    assert result['replacement_concurrency_equivalence'] == 'unverified'
    assert result['rejections'] == []


def test_retirement_is_a_terminal_returned_pair_outside_backend_call():
    source = rejection_fixture()
    source.pop()
    source.pop(2)
    for sequence, row in enumerate(source, 1):
        row['payload']['payload']['detail']['sequence'] = sequence
    for edge in ['stage-enter', 'stage-return']:
        source.append(dict(payload=dict(type='send', payload=dict(event='pn_backend_timeline',
            detail=dict(sequence=len(source) + 1, edge=edge, thread=47, owner=None, stage='jni:native-unbind')))))
    assert backend_timeout(source)['observed_prefix'] == 'closed'
    for mutate in [lambda rows: rows[-1]['payload']['payload']['detail'].update(thread=29),
                   lambda rows: rows[-1]['payload']['payload']['detail'].update(edge='stage-throw'),
                   lambda rows: rows[-1]['payload']['payload']['detail'].update(stage='jni:native-get'),
                   lambda rows: rows.pop()]:
        changed = deepcopy(source)
        mutate(changed)
        with pytest.raises((ValueError, KeyError)):
            backend_timeout(changed)


@pytest.mark.skipif(not os.environ.get('NASHURA_E21N_MANIFEST'), reason='private native attachments not distributed')
def test_private_native_diagnostics():
    path = Path(os.environ['NASHURA_E21N_MANIFEST'])
    result = evaluate(json.loads(path.read_text()), path.parent)
    assert result['status'] == 'bounded_diagnostics_verified'
    assert result['strict_gate'] == 'BLOCKED'
    assert result['no_vm_verified'] is False


@pytest.mark.skipif(not os.environ.get('NASHURA_E21N_MANIFEST'), reason='private native attachments not distributed')
@pytest.mark.parametrize('mutation', ['gate-counter', 'native-error', 'dispatch-stream', 'backend-stream'])
def test_private_serialized_evidence_rejects_corruption(mutation):
    path = Path(os.environ['NASHURA_E21N_MANIFEST'])
    manifest = json.loads(path.read_text())
    for kind in ['serialized_replacement', 'serialized_replacement_repeat']:
        run = path.parent / manifest['runs'][kind]
        bridge = json.loads((run / 'pn-bridge-final.json').read_text())
        messages = [json.loads(line) for line in (run / 'messages.jsonl').read_text().splitlines()]
        assert serial_dispatch(bridge, messages)['dispatch_calls'] == 154
        if mutation == 'gate-counter':
            bridge['dispatch_gate']['counters']['completed'] -= 1
        elif mutation == 'native-error':
            bridge['native_backend']['errors'].append('deliberate evidence corruption')
        elif mutation == 'dispatch-stream':
            bridge['dispatch_diagnostics']['events'][0]['phase'] = 'deliberate evidence corruption'
        else:
            bridge['native_backend']['diagnostics']['events'][0]['thread'] += 1
        with pytest.raises((ValueError, KeyError)):
            serial_dispatch(bridge, messages)


def test_cross_run_identity_allows_only_bound_added_gate():
    gate = '/fixture/libe21dispatchgate.so'
    source = dict(files={'/fixture/libg.so': 'engine', '/fixture/observer.dex': 'observer'},
                  update_manifest_sha256='updates')
    replacement = deepcopy(source)
    replacement['files'][gate] = 'gate'
    cross_run_identities([source, replacement, deepcopy(replacement)], [gate, gate])
    for mutation in ['engine', 'observer', 'extra', 'missing', 'updates', 'gate']:
        changed = deepcopy(replacement)
        if mutation in {'engine', 'observer'}:
            changed['files']['/fixture/' + ('libg.so' if mutation == 'engine' else 'observer.dex')] = 'changed'
        elif mutation == 'extra':
            changed['files']['/fixture/unknown.so'] = 'unknown'
        elif mutation == 'missing':
            changed['files'].pop('/fixture/observer.dex')
        elif mutation == 'updates':
            changed['update_manifest_sha256'] = 'changed'
        else:
            changed['files'][gate] = 'changed'
        with pytest.raises(ValueError):
            cross_run_identities([source, replacement, changed], [gate, gate])
    with pytest.raises(ValueError):
        cross_run_identities([source, replacement], ['/fixture/unknown.so'])
    with pytest.raises(ValueError):
        cross_run_identities([source, replacement], [gate, '/other/libe21dispatchgate.so'])


def serial_fixture():
    messages = rejection_fixture()
    messages.pop()
    messages.pop(2)
    for sequence, row in enumerate(messages, 1):
        row['payload']['payload']['detail']['sequence'] = sequence
    backend_events = [deepcopy(row['payload']['payload']['detail']) for row in messages]
    dispatch_events = []
    for call, thread in [(1, 11), (2, 29)]:
        for stage, edge in [('dispatch', 'enter'), ('native-read', 'enter'),
                            ('native-read', 'return'), ('dispatch', 'return')]:
            dispatch_events.append(dict(sequence=len(dispatch_events) + 1, call=call, thread=thread,
                operation='read', phase='explicit_fixture', wall_milliseconds=1, stage=stage, edge=edge))
    messages.extend(dict(payload=dict(type='send', payload=dict(event='pn_dispatch_timeline', detail=deepcopy(row))))
                    for row in dispatch_events)
    bridge = dict(dispatch_gate=dict(mode='native-serial-v1',
        scope='all native configuration bridge dispatches serialized before JavaScript; general equivalence unknown',
        counters=dict(entered=2, completed=2, active=0, waiting=0, peak_waiting=1, lock_errors=0)),
        dispatch_diagnostics=dict(mode='entry-return-v1', dropped=0, calls=2, events=dispatch_events),
        fallbacks=1, events=[dict(status=0, receipt=dict(failure=0))], errors=[], retired=True,
        installed=False, no_vm_verified=False, strict_gate='BLOCKED',
        native_backend=dict(errors=[], retired=True, ready=False, derivations=1, events=[dict(status='returned')],
            diagnostics=dict(schema='e2.1n-backend-timeline.v1', events=backend_events,
                dropped=0, busy=False, owner=None, general_thread_safety='unknown')))
    return bridge, messages


def test_serial_fixture_is_bounded_engineering_evidence():
    bridge, messages = serial_fixture()
    result = serial_dispatch(bridge, messages)
    assert result['dispatch_calls'] == 2
    assert result['general_thread_safety'] == 'unknown'
    assert result['replacement_concurrency_equivalence'] == 'unverified outside bounded serialized experiment'


@pytest.mark.parametrize('mutation', ['counter', 'waiting', 'lock-error', 'scope', 'dropped', 'accounting',
    'retired', 'installed', 'no-vm', 'strict', 'overlap', 'thread', 'throw', 'unclosed',
    'backend-error', 'backend-ready', 'backend-dropped', 'backend-stream', 'dispatch-stream'])
def test_serial_evidence_rejects_incomplete_or_overclaimed_state(mutation):
    bridge, messages = serial_fixture()
    diagnostic = bridge['dispatch_diagnostics']
    if mutation == 'counter':
        bridge['dispatch_gate']['counters']['completed'] = 1
    elif mutation in {'waiting', 'lock-error'}:
        bridge['dispatch_gate']['counters']['waiting' if mutation == 'waiting' else 'lock_errors'] = 1
    elif mutation == 'scope':
        bridge['dispatch_gate']['scope'] = 'all concurrency verified'
    elif mutation == 'dropped':
        diagnostic['dropped'] = 1
    elif mutation == 'accounting':
        bridge['fallbacks'] = 2
    elif mutation in {'retired', 'installed', 'no-vm', 'strict'}:
        bridge[{'no-vm': 'no_vm_verified', 'strict': 'strict_gate'}.get(mutation, mutation)] = (
            'PASS' if mutation == 'strict' else mutation != 'retired')
    elif mutation == 'overlap':
        diagnostic['events'][1].update(stage='dispatch', edge='enter', call=2)
    elif mutation == 'thread':
        diagnostic['events'][2]['thread'] = 29
    elif mutation == 'throw':
        diagnostic['events'][2]['edge'] = 'throw'
    elif mutation == 'unclosed':
        diagnostic['events'].pop()
    elif mutation == 'backend-error':
        bridge['native_backend']['errors'] = ['fixture error']
    elif mutation == 'backend-ready':
        bridge['native_backend']['ready'] = True
    elif mutation == 'backend-dropped':
        bridge['native_backend']['diagnostics']['dropped'] = 1
    elif mutation == 'backend-stream':
        bridge['native_backend']['diagnostics']['events'][0]['thread'] = 29
    else:
        diagnostic['events'][0]['phase'] = 'different'
    if mutation not in {'backend-stream', 'dispatch-stream'}:
        streamed = [row for row in messages if row['payload']['payload']['event'] != 'pn_dispatch_timeline']
        streamed.extend(dict(payload=dict(type='send', payload=dict(event='pn_dispatch_timeline', detail=deepcopy(row))))
                        for row in diagnostic['events'])
        messages = streamed
    with pytest.raises((ValueError, KeyError)):
        serial_dispatch(bridge, messages)
