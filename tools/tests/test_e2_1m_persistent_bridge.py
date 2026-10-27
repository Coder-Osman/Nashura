import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from verify_e0_snapshots import verify
from verify_e2_gate import resources
from verify_e2_1m_persistent_bridge import evaluate, lifecycle, validate_bridge, validate_pair


def private_root():
    value = os.environ.get('E2_1M_PRIVATE_MANIFEST')
    if not value:
        pytest.skip('requires private original/replacement complete native evidence')
    return Path(value).parent


def load(relative):
    return json.loads((private_root() / relative).read_text())


def pair(suffix):
    item = next(row for row in load('private-manifest.json')['pairs'] if row['suffix'] == suffix)
    return [load(item[side] + '/pn-edge-matrix.json') for side in ['original', 'replacement']] + [
        load(item[side] + '/private-config.json')['semantics_probe'] for side in ['original', 'replacement']]


def bridge():
    prefix = 'replacement-edges/'
    return [load(prefix + name + '.json') for name in ['return-observer-cold', 'return-observer-final',
        'pn-bridge-install', 'pn-bridge-final', 'return-observer-uninstall', 'return-observer-release', 'e2-final']]


def test_actual_twenty_complete_trajectories_remain_bounded():
    result = evaluate(load('private-manifest.json'), private_root())
    assert result['status'] == 'bounded_replacement_verified'
    assert len(result['pairs']) == 5
    assert sum(row['complete_trajectories'] for row in result['pairs']) == 20
    assert result['strict_gate'] == 'BLOCKED' and result['removed_keys'] == 0
    assert result['no_vm_verified'] is result['android_free_verified'] is result['training_admissible'] is False
    assert result['concurrency_equivalence'] == result['backup_delivery'] == 'unknown'
    text = json.dumps(result)
    assert '/Users/' not in text and '/data/' not in text and 'bytes_hex' not in text


@pytest.mark.parametrize('suffix', ['edges', 'malformed-first', 'padding-first', 'short-state', 'padding-state'])
def test_actual_ordered_persistent_state_differential(suffix):
    original, replacement, first, second = pair(suffix)
    result = validate_pair(original, replacement, suffix, first, second)
    assert result['restored_semantically'] is True


@pytest.mark.parametrize('mutation', ['order', 'native-result', 'thread', 'route', 'phase', 'bridge-prefix',
    'bridge-error', 'backend-prefix', 'backend-thread', 'backend-receipt', 'commit', 'xml', 'raw',
    'restore', 'promotion', 'no-vm', 'concurrency'])
def test_actual_edge_corruptions_rejected(mutation):
    original, replacement, first, second = pair('edges')
    row = replacement['cases'][1]
    event = row['bridge_after']['events'][-1]
    if mutation == 'order':
        replacement['cases'].reverse()
    elif mutation == 'native-result':
        row['native']['result']['value'] = 'invented'
    elif mutation == 'thread':
        row['native']['thread'] = 0
    elif mutation == 'route':
        event['route'] = 'original-fallback'
    elif mutation == 'phase':
        event['phase'] = 'unknown'
    elif mutation == 'bridge-prefix':
        row['bridge_after']['events'].pop(0)
    elif mutation == 'bridge-error':
        row['bridge_after']['errors'].append('unexpected')
    elif mutation == 'backend-prefix':
        row['bridge_after']['native_backend']['events'].pop(0)
    elif mutation == 'backend-thread':
        row['bridge_after']['native_backend']['events'][-1]['thread'] = 0
    elif mutation == 'backend-receipt':
        event['receipt']['reads'] += 1
    elif mutation == 'commit':
        row['events_after'] = row['events_after'].replace('commit:exit\ttrue', 'commit:exit\tfalse')
    elif mutation == 'xml':
        row['xml_after'][0]['sha256'] = '0' * 64
    elif mutation == 'raw':
        row['raw_after'][0]['encoded'] = 'invented'
    elif mutation == 'restore':
        replacement['restored'] = False
    elif mutation == 'promotion':
        replacement['strict_gate'] = 'PASS'
    elif mutation == 'no-vm':
        replacement['no_vm_verified'] = True
    else:
        replacement['concurrency_equivalence'] = 'verified'
    with pytest.raises(ValueError):
        validate_pair(original, replacement, 'edges', first, second)


@pytest.mark.parametrize('mutation', ['natural-caller', 'natural-thread', 'natural-return', 'natural-receipt',
    'raw-operations', 'upper-caller', 'dropped-config', 'decoder-mode', 'unsupported', 'released', 'fallback'])
def test_actual_natural_bridge_attribution_corruptions_rejected(mutation):
    args = bridge()
    cold, final, installed, state, uninstall, release, observed = args
    if mutation == 'natural-caller':
        state['events'][0]['caller'] = 'external-probe'
    elif mutation == 'natural-thread':
        final['cold']['windows'][0]['thread'] = 0
    elif mutation == 'natural-return':
        state['events'][0]['return_site'] = '0x0'
    elif mutation == 'natural-receipt':
        state['events'][1]['receipt']['backup_method_returns'] = 0
    elif mutation == 'raw-operations':
        final['cold']['windows'][1]['after'] = final['cold']['windows'][1]['before']
    elif mutation == 'upper-caller':
        next(row for row in observed['config_calls'] if row['operation'] == 'PreferenceRead'
             and row['key'].get('value') == 'PN_POPUP')['caller'] = 'unknown'
    elif mutation == 'dropped-config':
        observed['dropped']['config_calls'] = 1
    elif mutation == 'decoder-mode':
        state['native_backend']['mode'] = 'present-ciphertext-v1'
    elif mutation == 'unsupported':
        state['native_backend']['unsupported'].remove('concurrency')
    elif mutation == 'released':
        final['references']['deleted_globals'] = 0
    else:
        state['fallbacks'] += 1
    with pytest.raises(ValueError):
        validate_bridge(*args, 19)


def test_actual_missing_post_edge_snapshot_rejected(tmp_path):
    root = private_root() / 'replacement-edges'
    receipt = load('replacement-edges/post-edge-trace.json')
    with pytest.raises(ValueError, match='exactly the expected ticks'):
        verify(root / 'snapshots', tmp_path, load('replacement-edges/trace.json'), receipt)


def test_actual_post_edge_reset_receipt_required():
    rows = load('replacement-edges/control-receipts.json')
    lifecycle(rows, True)
    rows = [row for row in rows if row['phase'] != 'post_edge_terminal_reset_destroy']
    with pytest.raises(ValueError, match='complete native command lifecycle'):
        lifecycle(rows, True)


@pytest.mark.parametrize('mutation', ['asset-event', 'resource-slot', 'fmod', 'failure', 'live'])
def test_actual_resource_regressions_rejected(mutation):
    counts, redirect, fmod = [load('replacement-edges/' + name + '.json') for name in
                              ['e0-final', 'resource-final', 'fmod-final']]
    if mutation == 'asset-event':
        row = dict(counts['counts'][0])
        row.update(count=1, labels=['AAssetManager_fromJava'])
        counts['counts'].append(row)
    elif mutation == 'resource-slot':
        redirect['receipt']['redirects'][0]['verified'] = False
    elif mutation == 'fmod':
        next(row for row in fmod['events'] if row['kind'] == 'redirected_binding')['verified'] = False
    elif mutation == 'failure':
        redirect['metrics']['failures'] = '1'
    else:
        redirect['metrics']['live_assets'] = '1'
    with pytest.raises(ValueError):
        resources(counts, redirect, fmod)


def test_actual_attachment_tampering_rejected():
    manifest = load('private-manifest.json')
    manifest['files']['build/libe21m_bridge.so'] = '0' * 64
    with pytest.raises(ValueError, match='attachment hash differs'):
        evaluate(manifest, private_root())
