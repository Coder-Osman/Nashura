import copy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e2_1_gate import bridge_semantics, evaluate, pn_cold_chain, replacement_events, string_lifecycle


@pytest.fixture(scope='module')
def native_evidence():
    location = os.environ.get('E2_1_PRIVATE_MANIFEST')
    if not location:
        pytest.skip('private native attachments absent; no native equivalence inferred')
    path = Path(location)
    manifest = json.loads(path.read_text())
    return manifest, path.parent


def attachment(evidence, name):
    manifest, base = evidence
    raw = (base / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest['files'][name]
    return json.loads(raw)


def test_no_summary_only_acceptance():
    with pytest.raises(ValueError, match='wrong manifest schema'):
        evaluate({}, Path('.'))


@pytest.mark.parametrize('relative', ['../outside', '/absolute'])
def test_unsafe_attachment_rejected(relative):
    with pytest.raises(ValueError, match='unsafe attachment'):
        evaluate(dict(schema='e2.1-private-manifest.v1', files={relative: '0' * 64}), Path('.'))


def test_tampered_attachment_rejected(tmp_path):
    path = tmp_path / 'evidence.json'
    path.write_text('{"status":"pass"}')
    with pytest.raises(ValueError, match='hash mismatch'):
        evaluate(dict(schema='e2.1-private-manifest.v1', files={path.name: '0' * 64}), tmp_path)


def test_native_complete_comparisons_stay_blocked(native_evidence):
    manifest, base = native_evidence
    result = evaluate(manifest, base)
    assert result['bounded_native_comparisons'] == 'passed'
    assert result['status'] == 'blocked' and result['exit_code'] == 2
    assert result['required_unreplaced_count'] == 46
    assert result['unknown_key_negative'] == 'verified'
    assert all(row['comparison']['snapshot_count'] == 3602 for row in result['runs'])
    assert not result['no_vm_verified'] and not result['training_admissible']


@pytest.mark.parametrize('mutation', ['value', 'route', 'status', 'attribution', 'fallback', 'counter'])
def test_native_replacement_evidence_mutation_rejected(native_evidence, mutation):
    receipt = attachment(native_evidence, 'matrix-4-replacement/replacement-final.json')
    config = attachment(native_evidence, 'matrix-4-replacement/private-config.json')
    probe = attachment(native_evidence, 'matrix-4-replacement/semantics-final.json')
    assert replacement_events(receipt, config, probe)['targeted_reads'] == 32
    receipt = copy.deepcopy(receipt)
    event = next(row for row in receipt['events'] if row['targeted'] and row['operation'] == 'read')
    if mutation == 'value':
        event['actual'] += 'tampered'
    elif mutation == 'route':
        event['route'] = 'original_fallback'
    elif mutation == 'status':
        event['error'] = 'unexpected original error'
    elif mutation == 'attribution':
        event['targeted'] = False
    elif mutation == 'fallback':
        next(row for row in receipt['events'] if row['route'] == 'original_fallback')['key'] = 'UNDECLARED'
    else:
        receipt['store']['reads'] = '0'
    with pytest.raises(ValueError):
        replacement_events(receipt, config, probe)


@pytest.mark.parametrize('mutation', ['roundtrip', 'bytes', 'exception', 'reset'])
def test_native_bridge_semantics_mutation_rejected(native_evidence, mutation):
    data = attachment(native_evidence, 'matrix-6-strict-lifecycle/semantics-final.json')
    assert bridge_semantics(data)['pn_native_replacement'] is False
    rows = {row['case']: row for row in data['results']}
    if mutation == 'roundtrip':
        rows['empty_write']['input'] = 'changed'
    elif mutation == 'bytes':
        rows['utf8_read']['result']['bytes'][0] ^= 1
    elif mutation == 'exception':
        rows['utf8_read']['exception_state']['after'] = True
    else:
        row = rows['after_reset']['result']
        row.update(value='changed', bytes=list(b'changed'), length=7)
    with pytest.raises(ValueError):
        bridge_semantics(data)


@pytest.mark.parametrize('mutation', ['free', 'missing', 'length', 'event'])
def test_native_owned_string_lifecycle_mutation_rejected(native_evidence, mutation):
    manifest, _ = native_evidence
    data = attachment(native_evidence, manifest['string_lifecycle'])
    string_lifecycle(data)
    if mutation == 'free':
        data['rows'][-1]['frees_observed'] = 0
    elif mutation == 'missing':
        data['rows'].pop()
    elif mutation == 'length':
        data['rows'][-1]['length'] = 8
    else:
        data['events'].pop()
    with pytest.raises(ValueError):
        string_lifecycle(data)


@pytest.mark.parametrize('mutation', ['input', 'writeback', 'order', 'exists', 'restoration'])
def test_native_cold_chain_mutation_rejected(native_evidence, mutation):
    manifest, _ = native_evidence
    series = [dict(probe=attachment(native_evidence, run + '/semantics-final.json'),
                   observer=attachment(native_evidence, run + '/e2-final.json'))
              for run in manifest['cold_start_runs']]
    assert pn_cold_chain(series)['original_cold_transitions'] == 3
    natural = [row for row in series[1]['observer']['config_calls']
               if row['key'].get('value') == 'PN_POPUP'
               and row['operation'] in {'PreferenceNativeReadBridge', 'PreferenceNativeWriteBridge'}]
    if mutation == 'input':
        natural[0]['returned_string']['value'] = 'tampered'
    elif mutation == 'writeback':
        natural[1]['value']['value'] = '2'
    elif mutation == 'order':
        natural[1]['sequence'] = natural[0]['sequence'] - 1
    else:
        rows = {row['case']: row for row in series[0 if mutation == 'exists' else 3]['probe']['results']}
        if mutation == 'exists':
            rows['removed_exists']['result']['boolean'] = True
        else:
            rows['restore_saved']['input'] = 'tampered'
    with pytest.raises(ValueError):
        pn_cold_chain(series)
