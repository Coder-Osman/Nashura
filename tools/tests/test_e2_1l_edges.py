import copy
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_pn_edge_matrix import run_edge_matrix
from verify_e2_1l_edges import RUNS, evaluate, validate_direct, validate_matrix


def private_manifest():
    value = os.environ.get('E2_1L_PRIVATE_MANIFEST')
    if not value:
        pytest.skip('requires private complete PN edge/Cipher/EVP evidence')
    return Path(value)


def matrix(label='original-edges'):
    root = private_manifest().parent / label
    return json.loads((root / 'pn-edge-matrix.json').read_text()), json.loads(
        (root / 'private-config.json').read_text())['semantics_probe']


def test_engineering_stream_never_silently_replaces_original_bridge():
    with pytest.raises(ValueError, match='separate original-Cipher'):
        run_edge_matrix(None, None, True, None, None, direct_cipher=True, stream=object())
    with pytest.raises(ValueError, match='separate original-Cipher'):
        run_edge_matrix(None, None, False, None, None, stream=object())


def test_actual_complete_differential_evidence_remains_bounded():
    path = private_manifest()
    result = evaluate(json.loads(path.read_text()), path.parent)
    assert result['status'] == 'bounded_diagnostic_verified'
    assert [row['run'] for row in result['runs']] == RUNS
    assert result['strict_gate'] == 'BLOCKED' and result['removed_keys'] == 0
    assert result['no_vm_verified'] is result['android_free_verified'] is result['training_admissible'] is False
    assert result['abnormal_state_followed_by_complete_match'] == 'not tested'
    assert result['concurrency_equivalence'] == 'unknown'
    for row in result['runs'][-2:]:
        assert row['direct']['native_mismatches'] == []
        assert row['direct']['padding_recovery_bytes'] == [32, 16]
        assert row['direct']['native_exception_class_equivalence'].startswith('unknown')
    text = json.dumps(result)
    assert '/Users/' not in text and '/data/' not in text and 'bytes_hex' not in text


@pytest.mark.parametrize('label', RUNS[1:])
def test_actual_ordered_native_edge_cases(label):
    evidence, config = matrix(label)
    result = validate_matrix(evidence, label, config)
    assert result['restored_semantically'] is True


@pytest.mark.parametrize('mutation', ['order', 'case', 'replacement', 'restored', 'scope', 'gate',
    'no-vm', 'concurrency', 'xml-sha', 'raw-after', 'fixture', 'pending-exception', 'native-result',
    'return-type', 'offset', 'thread', 'drop', 'commits', 'restore-xml'])
def test_actual_edge_evidence_corruptions_rejected(mutation):
    evidence, config = matrix()
    row = evidence['cases'][1]
    if mutation == 'order':
        evidence['cases'].reverse()
    elif mutation == 'case':
        row['case'] = 'invented-case'
    elif mutation == 'replacement':
        evidence['replacement'] = True
    elif mutation == 'restored':
        evidence['restored'] = False
    elif mutation == 'scope':
        evidence['scope'] = 'natural cold-start equivalence'
    elif mutation == 'gate':
        evidence['strict_gate'] = 'PASS'
    elif mutation == 'no-vm':
        evidence['no_vm_verified'] = True
    elif mutation == 'concurrency':
        evidence['concurrency_equivalence'] = 'verified'
    elif mutation == 'xml-sha':
        row['xml_after'][0]['sha256'] = '0' * 64
    elif mutation == 'raw-after':
        row['raw_after'][1]['encoded'] = None
    elif mutation == 'fixture':
        row['fixtures'][0]['plain'] = 'injected'
    elif mutation == 'pending-exception':
        row['native']['exception_state']['after'] = True
    elif mutation == 'native-result':
        row['native']['result']['value'] = 'fabricated'
    elif mutation == 'return-type':
        evidence['cases'][-1]['native']['return_type'] = 'integer status'
    elif mutation == 'offset':
        config['read_bridge'] += 4
    elif mutation == 'thread':
        row['native']['thread'] = 0
    elif mutation == 'drop':
        row['events_after'] = row['events_after'].replace('dropped\t0', 'dropped\t1')
    elif mutation == 'commits':
        row['events_after'] = row['events_after'].replace('commit:exit\ttrue', 'commit:exit\tfalse')
    else:
        evidence['xml_restored'][0] = copy.deepcopy(evidence['cases'][0]['xml_after'][0])
    with pytest.raises((ValueError, KeyError)):
        validate_matrix(evidence, 'original-edges', config)


@pytest.mark.parametrize('mutation', ['order', 'exception', 'length', 'valid-input', 'bad-input',
    'native-error', 'native-output', 'native-status', 'closed', 'call-count', 'close-error', 'no-vm'])
def test_actual_persistent_decoder_corruptions_rejected(mutation):
    evidence, unused = matrix('original-stream-diff-3')
    rows = evidence['direct_cipher']
    if mutation == 'order':
        rows.reverse()
    elif mutation == 'exception':
        rows[1]['result']['exception'] = 'javax.crypto.BadPaddingException'
    elif mutation == 'length':
        rows[6]['result']['bytes'] = 16
    elif mutation == 'valid-input':
        rows[3]['input'] = rows[1]['input']
    elif mutation == 'bad-input':
        rows[1]['input'] = rows[0]['input']
    elif mutation == 'native-error':
        rows[1]['stream']['status'] = 0
    elif mutation == 'native-output':
        rows[6]['stream']['output'][-1] ^= 1
    elif mutation == 'native-status':
        rows[0]['stream']['status'] = True
    elif mutation == 'closed':
        evidence['stream_close']['closed'] = False
    elif mutation == 'call-count':
        evidence['stream_close']['calls'][0] = 3
    elif mutation == 'close-error':
        evidence['stream_close_error'] = 'ignored'
    else:
        evidence['stream_close']['no_vm_verified'] = True
    with pytest.raises(ValueError):
        validate_direct(evidence, True)


def test_actual_unknown_run_label_and_mutated_attachment_rejected():
    evidence, config = matrix()
    with pytest.raises(ValueError, match='unknown diagnostic'):
        validate_matrix(evidence, 'unobserved-run', config)
    path = private_manifest()
    manifest = json.loads(path.read_text())
    manifest['files']['baseline/run-start.json'] = '0' * 64
    with pytest.raises(ValueError, match='attachment hash'):
        evaluate(manifest, path.parent)
