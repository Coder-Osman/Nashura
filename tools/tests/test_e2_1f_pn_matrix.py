import copy
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_pn_matrix import CASES
from verify_e2_1f_pn_matrix import evaluate, validate_matrix


def private_manifest():
    value = os.environ.get('E2_1F_PRIVATE_MANIFEST')
    if not value:
        pytest.skip('requires actual private original bridge priority/backfill evidence')
    return Path(value)


def actual_matrix(run='matrix-1'):
    base = private_manifest().parent / run
    return [json.loads((base / name).read_text()) for name in
            ['pn-matrix.json', 'pn-matrix-candidate-final.json']]


def test_actual_manifest_and_public_scope():
    path = private_manifest()
    result = evaluate(json.loads(path.read_text()), path.parent)
    assert result['status'] == 'bounded_evidence_verified' and len(result['runs']) == 3
    assert result['strict_gate'] == 'BLOCKED' and result['pn_native_replacement'] is False
    assert result['no_vm_verified'] is False and result['removed_keys'] == 0
    serialized = json.dumps(result)
    assert '/Users/' not in serialized and '/data/' not in serialized
    for item in CASES:
        for value in item['initial'] + [item.get('value', '')]:
            if value:
                assert value not in serialized


@pytest.mark.parametrize('run', ['matrix-1', 'matrix-2'])
def test_actual_original_bridge_and_candidate_matrix(run):
    matrix, final = actual_matrix(run)
    result = validate_matrix(matrix, final)
    assert result['cases'] == 11 and result['reads'] == 7 and result['writes'] == 4
    assert result['raw_entries_restored'] is True and result['backup_delivery'] == 'unknown'


@pytest.mark.parametrize('mutation', ['empty', 'order', 'setup', 'store-order', 'plain', 'key',
    'offset', 'entry', 'exception', 'thread', 'bytes', 'length', 'read-value', 'input', 'return-type',
    'after-value', 'after-encoded', 'unchanged-encoded', 'candidate-status', 'candidate-close',
    'candidate-disk', 'candidate-receipt', 'candidate-order', 'window', 'commit', 'restore',
    'restore-receipt', 'promotion', 'format', 'backup'])
def test_real_matrix_rejects_incomplete_or_mutated_evidence(mutation):
    matrix, final = actual_matrix()
    rows = matrix['cases']
    if mutation == 'empty':
        rows.clear()
    elif mutation == 'order':
        rows.reverse()
    elif mutation == 'setup':
        rows[1]['before'][0]['value'] = 'changed'
    elif mutation == 'store-order':
        rows[1]['after'].reverse()
    elif mutation == 'plain':
        rows[1]['after'][0]['plain'] = 'unexpected clear text'
    elif mutation == 'key':
        rows[1]['original']['key'] = 'OTHER'
    elif mutation == 'offset':
        rows[1]['original']['entry_offset'] += 4
    elif mutation == 'entry':
        rows[1]['original']['entry'] = 'write_bridge'
    elif mutation == 'exception':
        rows[1]['original']['exception_state']['after'] = True
    elif mutation == 'thread':
        rows[1]['original']['thread'] = 0
    elif mutation == 'bytes':
        rows[5]['original']['result']['bytes'] = [0]
    elif mutation == 'length':
        rows[5]['original']['result']['length'] += 1
    elif mutation == 'read-value':
        rows[5]['original']['result']['value'] = 'changed'
    elif mutation == 'input':
        rows[7]['original']['input'] = 'changed'
    elif mutation == 'return-type':
        rows[7]['original']['return_type'] = 'status inferred from register'
    elif mutation == 'after-value':
        rows[1]['after'][1]['value'] = ''
    elif mutation == 'after-encoded':
        rows[1]['after'][1].pop('encoded')
    elif mutation == 'unchanged-encoded':
        rows[3]['after'][0]['encoded'] = 'mutated original raw entry'
    elif mutation == 'window':
        rows[1]['events_after'] = rows[1]['events_before']
    elif mutation == 'commit':
        rows[1]['events_after'] = rows[1]['events_after'].replace('commit:exit\ttrue', 'commit:exit\tfalse')
    elif mutation == 'restore':
        matrix['restored'] = False
    elif mutation == 'restore-receipt':
        matrix['restore_receipt'] = 'pn-restored\t1'
    elif mutation == 'promotion':
        matrix['native_bridge_replacement'] = True
    elif mutation == 'format':
        final['original_storage_format_compatibility'] = True
    elif mutation == 'backup':
        matrix['backup_delivery'] = 'verified'
    else:
        candidate = copy.deepcopy(rows[1]['candidate'])
        if mutation == 'candidate-status':
            candidate['status'] = -1
        elif mutation == 'candidate-close':
            candidate['close_status'] = -1
        elif mutation == 'candidate-disk':
            candidate['disk'] = ['', '']
        elif mutation == 'candidate-receipt':
            candidate['receipt']['committed'] = 0
        elif mutation == 'candidate-order':
            candidate['case'] = 'other'
        rows[1]['candidate'] = candidate
        final['results'][1] = candidate
    with pytest.raises(ValueError):
        validate_matrix(matrix, final)


@pytest.mark.parametrize('relative', ['../outside', '/absolute'])
def test_attachment_escape_rejected(tmp_path, relative):
    with pytest.raises(ValueError, match='unsafe attachment path'):
        evaluate(dict(schema='e2.1f-private-manifest.v1', files={relative: '0' * 64}), tmp_path)


def test_hash_and_symlink_rejected(tmp_path):
    path = tmp_path / 'fixture'
    path.write_text('engineering attachment fixture')
    manifest = dict(schema='e2.1f-private-manifest.v1', files={'fixture': '0' * 64})
    with pytest.raises(ValueError, match='attachment hash differs'):
        evaluate(manifest, tmp_path)
    (tmp_path / 'link').symlink_to(path)
    manifest['files'] = {'link': hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match='symlink attachment'):
        evaluate(manifest, tmp_path)


def test_missing_attachment_binding_rejected(tmp_path):
    manifest = dict(schema='e2.1f-private-manifest.v1', files={},
                    verification_sources={'E21PreferenceObserver.java': 'not-bound'})
    with pytest.raises(ValueError, match='unbound attachment'):
        evaluate(manifest, tmp_path)
