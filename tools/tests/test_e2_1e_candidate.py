import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e2_1e_candidate import CASES, evaluate, validate_replay


def private_manifest():
    value = os.environ.get('E2_1E_PRIVATE_MANIFEST')
    if not value:
        pytest.skip('requires actual private original-bridge and candidate native evidence')
    return Path(value)


def actual_replay():
    base = private_manifest().parent / 'candidate-3'
    return [json.loads((base / name).read_text()) for name in ['semantics-cases.json', 'pn-candidate-replay.json']]


def test_actual_native_manifest_and_public_fields():
    path = private_manifest()
    result = evaluate(json.loads(path.read_text()), path.parent)
    assert result['status'] == 'bounded_evidence_verified'
    assert len(result['runs']) == 2 and result['semantics']['equal_readbacks'] == 7
    assert result['strict_gate'] == 'BLOCKED' and result['pn_native_replacement'] is False
    assert result['no_vm_verified'] is False and result['removed_keys'] == 0
    serialized = json.dumps(result)
    assert '/Users/' not in serialized and '/data/' not in serialized
    for _, value in CASES:
        if value:
            assert value not in serialized


def test_actual_readbacks_and_commit_failure():
    original, candidate = actual_replay()
    result = validate_replay(original, candidate)
    assert result['equal_readbacks'] == 7 and result['real_permission_failure'] is True
    assert result['failed_write_not_persisted'] is True and result['close_rejections'] == 3


@pytest.mark.parametrize('mutation', ['empty-original', 'empty-candidate', 'order', 'key', 'offset',
    'exception', 'restoration', 'bytes', 'input', 'candidate-value', 'equal', 'commit', 'same',
    'read-status', 'candidate-exception', 'open-value', 'open-status', 'closed', 'permission',
    'errno', 'visible', 'retry', 'fault-reopen', 'missing-fault', 'final-close', 'promotion'])
def test_actual_replay_rejects_incomplete_or_inconsistent_evidence(mutation):
    original, replay = actual_replay()
    rows, experiments = replay['results'], replay['experiments']
    if mutation == 'empty-original':
        original.clear()
    elif mutation == 'empty-candidate':
        rows.clear()
    elif mutation == 'order':
        rows.reverse()
    elif mutation == 'key':
        original[1]['key'] = 'other'
    elif mutation == 'offset':
        original[1]['entry_offset'] += 4
    elif mutation == 'exception':
        original[1]['exception_state']['after'] = True
    elif mutation == 'restoration':
        original[-2]['input'] = 'PRIVATE_SENTINEL'
    elif mutation == 'bytes':
        original[4]['result']['bytes'] = [0]
    elif mutation == 'input':
        original[3]['input'] = 'PRIVATE_SENTINEL'
    elif mutation == 'candidate-value':
        rows[3]['candidate']['value'] = 'PRIVATE_SENTINEL'
    elif mutation == 'equal':
        rows[3]['equal'] = False
    elif mutation == 'commit':
        rows[2]['candidate']['receipt']['committed'] = 0
    elif mutation == 'same':
        rows[4]['candidate']['receipt']['changed'] = 3
    elif mutation == 'read-status':
        rows[1]['candidate']['status'] = -4
    elif mutation == 'candidate-exception':
        rows[0]['original_exception_state']['before'] = True
    elif mutation == 'open-value':
        experiments[1]['after']['value'] = 'PRIVATE_SENTINEL'
    elif mutation == 'open-status':
        experiments[1]['after']['status'] = -4
    elif mutation == 'closed':
        replay['closed'] = False
    elif mutation == 'permission':
        experiments[3]['failed']['receipt']['committed'] = 3
    elif mutation == 'errno':
        experiments[3]['failed']['receipt']['error_p'] = 0
    elif mutation == 'visible':
        experiments[3]['visible']['value'] = 'candidate-durable'
    elif mutation == 'retry':
        experiments[3]['same']['receipt']['changed'] = 3
    elif mutation == 'fault-reopen':
        experiments[5]['observed']['value'] = 'candidate-volatile'
    elif mutation == 'missing-fault':
        experiments.pop(3)
    elif mutation == 'final-close':
        experiments[-1]['status'] = 0
    else:
        replay['replacement_verified'] = True
    with pytest.raises(ValueError):
        validate_replay(original, replay)


@pytest.mark.parametrize('relative', ['../outside', '/absolute'])
def test_attachment_escape_rejected(tmp_path, relative):
    with pytest.raises(ValueError, match='unsafe attachment path'):
        evaluate(dict(schema='e2.1e-private-manifest.v1', files={relative: '0' * 64}), tmp_path)


def test_hash_and_symlink_rejected(tmp_path):
    path = tmp_path / 'fixture'
    path.write_text('engineering attachment fixture')
    manifest = dict(schema='e2.1e-private-manifest.v1', files={'fixture': '0' * 64})
    with pytest.raises(ValueError, match='attachment hash differs'):
        evaluate(manifest, tmp_path)
    (tmp_path / 'link').symlink_to(path)
    manifest['files'] = {'link': hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match='symlink attachment'):
        evaluate(manifest, tmp_path)


def test_missing_attachment_binding_rejected(tmp_path):
    manifest = dict(schema='e2.1e-private-manifest.v1', files={},
                    verification_sources={'e2_1_pn_store.c': 'not-bound'})
    with pytest.raises(ValueError, match='unbound attachment'):
        evaluate(manifest, tmp_path)
