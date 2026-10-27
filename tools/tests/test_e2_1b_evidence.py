import copy
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e2_1b_evidence import CASES, concurrent, evaluate, fresh_transition, sequential


def sequential_evidence():
    probes = []
    roots = []
    outputs = dict(normal_repeat='saved', changed_read='changed', empty_read='', restored_read='saved')
    inputs = dict(changed_write='changed', same_write='changed', empty_write='', restore='saved')
    for name in CASES:
        operation = 'write' if name in inputs else 'read'
        probe = dict(case=name, operation=operation, thread=17, exception_state=dict(before=False, after=False))
        if operation == 'write':
            probe['input'] = inputs[name]
        else:
            probe['result'] = dict(value=outputs[name])
        commits = 2 if name in {'changed_write', 'empty_write', 'restore'} else 0
        calls = [dict(method='fixture' + suffix) for suffix in [
            '.commit()Z', '.commitToMemory()Landroid/app/SharedPreferencesImpl$MemoryCommitResult;',
            '.writeToFile(Landroid/app/SharedPreferencesImpl$MemoryCommitResult;Z)V'] for _ in range(commits)]
        roots.append(dict(thread=3, method='storeKeyValue' if operation == 'write' else 'getKeyValue',
                          completion='returned', selected_calls=calls))
        probes.append(probe)
    return probes, dict(roots=roots, selected_counts={}, trace_sha256='fixture', records=100)


def concurrent_evidence():
    workers = [dict(thread=tid, completed=4, error=0, detached=True) for tid in [17, 18]]
    roots = [dict(thread=thread, method=method, completion='returned', selected_calls=[],
                  start_wall=index * 10, end_wall=index * 10 + 20)
             for index, method in enumerate(['storeKeyValue', 'getKeyValue'] * 4) for thread in [1, 2]]
    return dict(status=0, workers=workers), dict(roots=roots, trace_sha256='fixture'), dict(
        same_value_after=True, before_sha256='fixture', after_sha256='fixture')


def test_bounded_semantics_preserve_unknown_return_and_concurrency_scope():
    summary = sequential(*sequential_evidence())
    assert summary['commit_return_value'] == 'unknown'
    summary = concurrent(*concurrent_evidence())
    assert summary['write_calls'] == summary['read_calls'] == 8
    assert summary['overlapping_java_root_pairs'] > 0
    assert 'general thread safety unknown' in summary['scope']


@pytest.mark.parametrize('mutation', [None, 'read', 'write', 'order', 'thread', 'extra', 'route', 'scope'])
def test_fresh_transition_uses_observed_wrappers_and_separate_route_layer(mutation):
    wrappers = [dict(operation='PreferenceRead', key=dict(value='PN_POPUP'), thread=7, sequence=10,
                     returned_string=dict(status='decoded', value='117')),
                dict(operation='PreferenceWrite', key=dict(value='PN_POPUP'), thread=7, sequence=20,
                     value=dict(status='decoded', value='118'))]
    calls = [dict(key='PN_POPUP', operation=operation, thread=7, native_scope=1,
                  route='original_fallback', targeted=False) for operation in ['read', 'write']]
    if mutation == 'read':
        wrappers[0]['returned_string']['value'] = '999'
    elif mutation == 'write':
        wrappers[1]['value']['value'] = '999'
    elif mutation == 'order':
        wrappers.reverse()
    elif mutation == 'thread':
        calls[0]['thread'] = 8
    elif mutation == 'extra':
        wrappers.append(copy.deepcopy(wrappers[0]))
    elif mutation == 'route':
        calls[0]['route'] = 'native_read'
    elif mutation == 'scope':
        calls[0]['native_scope'] = None
    if mutation is None:
        assert fresh_transition(dict(config_calls=wrappers), dict(events=calls), '117')['status'] == 'verified'
    else:
        with pytest.raises(ValueError):
            fresh_transition(dict(config_calls=wrappers), dict(events=calls), '117')


@pytest.mark.parametrize('mutation', ['order', 'same', 'read', 'restore', 'exception', 'thread', 'root', 'commit', 'unwind', 'apply'])
def test_rejects_wrong_sequential_semantics(mutation):
    probes, trace = copy.deepcopy(sequential_evidence())
    if mutation == 'order':
        probes.reverse()
    elif mutation == 'same':
        probes[3]['input'] = 'different'
    elif mutation == 'read':
        probes[2]['result']['value'] = 'different'
    elif mutation == 'restore':
        probes[-1]['result']['value'] = 'different'
    elif mutation == 'exception':
        probes[0]['exception_state']['after'] = True
    elif mutation == 'thread':
        probes[-1]['thread'] = 19
    elif mutation == 'root':
        trace['roots'].pop()
    elif mutation == 'commit':
        trace['roots'][1]['selected_calls'].pop()
    elif mutation == 'unwind':
        trace['roots'][0]['completion'] = 'unwound'
    else:
        trace['selected_counts']['fixture.apply()V'] = 1
    with pytest.raises(ValueError):
        sequential(probes, trace)


@pytest.mark.parametrize('mutation', ['worker', 'detach', 'value', 'root', 'order', 'commit', 'unwind', 'overlap', 'wrap'])
def test_rejects_wrong_concurrent_semantics(mutation):
    result, trace, values = copy.deepcopy(concurrent_evidence())
    if mutation == 'worker':
        result['workers'][0]['completed'] = 3
    elif mutation == 'detach':
        result['workers'][0]['detached'] = False
    elif mutation == 'value':
        values['after_sha256'] = 'different'
    elif mutation == 'root':
        trace['roots'].pop()
    elif mutation == 'order':
        trace['roots'][0]['method'] = 'getKeyValue'
    elif mutation == 'commit':
        trace['roots'][0]['selected_calls'].append(dict(method='fixture.commit()Z'))
    elif mutation == 'unwind':
        trace['roots'][0]['completion'] = 'unwound'
    elif mutation == 'overlap':
        for index, row in enumerate(trace['roots']):
            row.update(start_wall=index * 100, end_wall=index * 100 + 1)
    else:
        trace['roots'][0]['start_wall'] = 1000
    with pytest.raises(ValueError):
        concurrent(result, trace, values)


@pytest.fixture(scope='module')
def private_manifest():
    path = os.environ.get('E2_1B_PRIVATE_MANIFEST')
    if not path:
        pytest.skip('requires private complete native E2.1-B attachments')
    target = Path(path)
    return json.loads(target.read_text()), target.resolve().parent


def test_complete_real_native_evidence(private_manifest):
    manifest, base = private_manifest
    result = evaluate(manifest, base)
    assert result['strict_gate'] == 'BLOCKED'
    assert len(result['runs']) == 5
    assert result['removability']['classification_counts'] == {'unknown': 46}
    assert result['no_vm_verified'] is result['training_admissible'] is False


@pytest.mark.parametrize('mutation', ['hash', 'unbound', 'duplicate', 'escape', 'mode'])
def test_real_attachment_manifest_rejects_tampering(private_manifest, mutation):
    source, base = private_manifest
    manifest = copy.deepcopy(source)
    if mutation == 'hash':
        manifest['files'][manifest['baseline'] + '/trace.json'] = '0' * 64
    elif mutation == 'unbound':
        manifest['files'].pop(manifest['baseline'] + '/trace.json')
    elif mutation == 'duplicate':
        manifest['runs'][-1] = manifest['runs'][0]
    elif mutation == 'escape':
        manifest['files']['../escape'] = '0' * 64
    else:
        manifest['runs'][1], manifest['runs'][2] = manifest['runs'][2], manifest['runs'][1]
    with pytest.raises(ValueError):
        evaluate(manifest, base)
