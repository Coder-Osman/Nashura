import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e2_1i_cold_pn_bridge import RUNS, evaluate, natural_values, validate_replacement


def test_engineering_installation_order_and_fail_closed():
    result = subprocess.run(['node', str(Path(__file__).with_name('e2_1i_cold_bridge_harness.js'))],
                            capture_output=True, text=True, check=True)
    assert 'engineering fixture passed; not native evidence' in result.stdout


def private_manifest():
    value = os.environ.get('E2_1I_PRIVATE_MANIFEST')
    if not value:
        pytest.skip('requires actual natural cold-start native PN replacement attachments')
    return Path(value)


def actual_replacement():
    base = private_manifest().parent
    run = base / RUNS[2]
    names = ['return-observer-cold.json', 'return-observer-final.json', 'pn-bridge-install.json',
             'pn-bridge-final.json', 'e2-final.json']
    values = [json.loads((run / name).read_text()) for name in names]
    values.append(json.loads((base / RUNS[1] / 'return-observer-final.json').read_text()))
    values.extend(json.loads((run / name).read_text()) for name in
                  ['return-observer-uninstall.json', 'return-observer-release.json'])
    return values


def test_engineering_decimal_increment_fixture_is_not_native_evidence():
    observed = dict(errors=[], dropped=dict(config_calls=0, errors=0), config_calls=[
        dict(operation='PreferenceRead', phase='configuration', key=dict(value='PN_POPUP'),
             returned_string=dict(status='decoded', value='8')),
        dict(operation='PreferenceWrite', phase='cold_ready_wait', key=dict(value='PN_POPUP'),
             value=dict(status='decoded', value='9'))])
    assert natural_values(observed) == ('8', '9')
    for value in ['8', '10', '', '９', '-1', 'unknown']:
        changed = copy.deepcopy(observed)
        changed['config_calls'][1]['value']['value'] = value
        with pytest.raises(ValueError):
            natural_values(changed)


def test_actual_manifest_remains_bounded_and_public():
    path = private_manifest()
    result = evaluate(json.loads(path.read_text()), path.parent)
    assert result['status'] == 'bounded_evidence_verified'
    assert result['strict_gate'] == 'BLOCKED' and result['removed_keys'] == 0
    assert result['independent_process_readback_equal'] is True
    assert result['no_vm_verified'] is result['original_xml_writer_replaced'] is False
    text = json.dumps(result)
    assert '/Users/' not in text and '/data/' not in text and 'returned_string' not in text


@pytest.mark.parametrize('mutation', ['missing-after', 'completion', 'caller', 'thread', 'base', 'return-site',
    'bridge-index', 'entry-order', 'external-count', 'natural-count', 'prefix', 'receipt', 'route', 'status',
    'read-value', 'write-value', 'external-phase', 'external-value', 'retire', 'references', 'fallback',
    'reentrant', 'install-entry', 'install-failed', 'upper-phase', 'upper-caller', 'upper-thread',
    'observer-dropped', 'commit', 'operation-order', 'scope', 'no-vm', 'backup', 'release'])
def test_actual_natural_window_corruptions_rejected(mutation):
    values = actual_replacement()
    initial, final, installed, bridge, observed, reference, uninstall, release = values
    cold = final['cold']
    window = cold['windows'][0]
    event = bridge['events'][0]
    if mutation == 'missing-after':
        window.pop('after')
    elif mutation == 'completion':
        window['completion'] = 'assumed'
    elif mutation == 'caller':
        window['caller'] = 'unknown'
    elif mutation == 'thread':
        event['thread'] += 1
    elif mutation == 'base':
        event['module_base'] = '0x1'
    elif mutation == 'return-site':
        event['return_site'] = event['module_base']
    elif mutation == 'bridge-index':
        window['bridge_event'] = 1
    elif mutation == 'entry-order':
        window['entry'] = cold['windows'][1]['entry'] + 1
    elif mutation == 'external-count':
        cold['external_windows'] += 1
    elif mutation == 'natural-count':
        cold['windows'].pop()
    elif mutation == 'prefix':
        installed['events'].reverse()
    elif mutation == 'receipt':
        bridge['events'][1]['receipt']['changed'] = 0
    elif mutation == 'route':
        event['route'] = 'original-fallback'
    elif mutation == 'status':
        event['status'] = -1
    elif mutation == 'read-value':
        event['result'] = 'different'
    elif mutation == 'write-value':
        bridge['events'][1]['input'] = 'different'
    elif mutation == 'external-phase':
        bridge['events'][-1]['phase'] = 'configuration'
    elif mutation == 'external-value':
        bridge['events'][-1]['result'] = 'different'
    elif mutation == 'retire':
        bridge['retired'] = False
    elif mutation == 'references':
        final['references']['deleted_globals'] = 0
    elif mutation == 'fallback':
        bridge['fallbacks'] += 1
    elif mutation == 'reentrant':
        cold['reentrant_entries'] = 1
    elif mutation == 'install-entry':
        cold['install_entry']['entry'] = 2
    elif mutation == 'install-failed':
        cold['installed'] = False
    elif mutation.startswith('upper-'):
        row = next(row for row in observed['config_calls'] if row['operation'] == 'PreferenceRead'
                   and row.get('key', {}).get('value') == 'PN_POPUP')
        key = mutation.split('-', 1)[1]
        row[key] = row[key] + 1 if key == 'thread' else 'unknown'
    elif mutation == 'observer-dropped':
        observed['dropped']['config_calls'] = 1
    elif mutation == 'commit':
        cold['windows'][1]['after'] = cold['windows'][1]['after'].replace('commit:exit\ttrue', 'commit:exit\tfalse')
    elif mutation == 'operation-order':
        reference['cold']['windows'][0]['after'] = reference['cold']['windows'][0]['before']
    elif mutation == 'scope':
        bridge['scope'] = 'all configuration removed'
    elif mutation == 'no-vm':
        bridge['no_vm_verified'] = True
    elif mutation == 'backup':
        bridge['backup_delivery'] = 'verified'
    elif mutation == 'release':
        release['result'] = 'released\t0'
    initial['cold']['windows'] = copy.deepcopy(cold['windows'])
    if mutation != 'prefix':
        installed['events'] = copy.deepcopy(bridge['events'][:3])
    with pytest.raises((ValueError, KeyError)):
        validate_replacement(*values)


@pytest.mark.parametrize('mutation', ['process', 'input-identity', 'source', 'snapshot', 'resource-live',
    'provider-failure', 'asset-event', 'build', 'dex', 'guest-artifact', 'configuration', 'cold-phase',
    'bridge-install', 'incomplete', 'action', 'cleanup', 'readback-exception', 'readback-source', 'readback-value'])
def test_actual_manifest_corruptions_rejected(monkeypatch, mutation):
    path = private_manifest()
    manifest = json.loads(path.read_text())
    run = RUNS[3] + '/'
    relative = run + {'process': 'owned-process.json', 'input-identity': 'identity-after.json',
        'source': 'sources/e2_1_pn_bridge.js', 'snapshot': 'snapshots/tick-000100.bin',
        'resource-live': 'resource-final.json', 'provider-failure': 'resource-final.json',
        'asset-event': 'e0-final.json', 'configuration': 'private-config.json', 'cold-phase': 'private-config.json',
        'bridge-install': 'pn-bridge-install.json', 'incomplete': 'run-finished.json',
        'action': 'control-receipts.json', 'cleanup': 'cleanup.json'}.get(mutation, 'unused')
    if mutation in ['build', 'dex', 'guest-artifact']:
        relative = 'guest-artifacts-after.json' if mutation == 'guest-artifact' else 'build-reproduction.json'
    if mutation.startswith('readback-'):
        relative = RUNS[2] + '/semantics-terminal-reset.json'
    target = path.parent / relative
    raw = target.read_bytes()
    if mutation == 'source':
        changed = raw + b'\n'
    elif mutation == 'snapshot':
        changed = bytes([raw[0] ^ 1]) + raw[1:]
    else:
        value = json.loads(raw)
        if mutation == 'process':
            value = json.loads((path.parent / RUNS[1] / 'owned-process.json').read_text())
        elif mutation == 'input-identity':
            value['update_manifest_sha256'] = '0' * 64
        elif mutation in ['resource-live', 'provider-failure']:
            value['metrics']['live_assets' if mutation == 'resource-live' else 'failures'] = '1'
        elif mutation == 'asset-event':
            value['counts'].append(dict(value['counts'][0], labels=['AAsset_read'], count=1))
        elif mutation in ['build', 'dex']:
            value['rebuilt_sha256' if mutation == 'build' else 'rebuilt_dex_sha256'] = '0' * 64
        elif mutation == 'guest-artifact':
            value['identities'][next(iter(value['identities']))] = '0' * 64
        elif mutation == 'configuration':
            value['mode'] = 'strict-targeted'
        elif mutation == 'cold-phase':
            value['pn_bridge']['install_at'] = 'post-cold-ready'
        elif mutation == 'bridge-install':
            value['retired'] = True
        elif mutation == 'incomplete':
            value['status'] = 'failed'
        elif mutation == 'action':
            next(row for row in value if row['phase'] == 'initial_state')['response']['ok'] = False
        elif mutation == 'cleanup':
            value['pid'] += 1
        elif mutation == 'readback-exception':
            value[0]['exception_state']['after'] = True
        elif mutation == 'readback-source':
            value[0]['exception_state']['source'] = 'assumed'
        elif mutation == 'readback-value':
            value[0]['result']['value'] = 'different'
        changed = json.dumps(value).encode()
    manifest['files'][relative] = hashlib.sha256(changed).hexdigest()
    actual_read = Path.read_bytes

    def read_attachment(candidate):
        return changed if candidate == target else actual_read(candidate)

    monkeypatch.setattr(Path, 'read_bytes', read_attachment)
    with pytest.raises((ValueError, KeyError)):
        evaluate(manifest, path.parent)
