import hashlib
import json
import os
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_backup_trace import parse_backup
from verify_e2_1h_pn_bridge import evaluate, validate_backup, validate_matrix


def private_manifest():
    value = os.environ.get('E2_1H_PRIVATE_MANIFEST')
    if not value:
        pytest.skip('requires original native bridge and replacement evidence')
    return Path(value)


def actual_matrix():
    base = private_manifest().parent
    original = json.loads((base / 'original-final/pn-bridge-matrix.json').read_text())
    matrix = json.loads((base / 'replacement-retained-1/pn-bridge-matrix.json').read_text())
    final = json.loads((base / 'replacement-retained-1/pn-bridge-final.json').read_text())
    return matrix, original, final


def trace_bytes(records=None):
    header = ('*version\n3\ndata-file-overflow=false\nclock=dual\nnum-method-calls=4\n*threads\n'
              '1\tfixture\n*methods\n0x4\tcom.supercell.titan.GameApp\trequestBackup\t()V\n'
              '0x8\tandroid.app.backup.BackupManager\tdataChanged\t()V\n*end\n').encode()
    binary = struct.pack('<4sHHQH', b'SLOW', 3, 32, 0, 14) + bytes(14)
    records = [(1, 4, 0, 1), (1, 8, 0, 2), (1, 9, 0, 3), (1, 5, 0, 4)] if records is None else records
    return header + binary + b''.join(struct.pack('<HIII', *record) for record in records)


def test_engineering_art_fixture_named_scope():
    result = parse_backup(trace_bytes())
    assert result['records'] == 4 and len(result['calls']) == 2
    assert result['unnamed_records'] == 0 and result['backup_delivery'] == 'unknown'
    assert result['calls'][0]['begin_sequence'] == 0 and result['calls'][0]['end_sequence'] == 3


@pytest.mark.parametrize('mutation', ['overflow', 'record-count', 'layout', 'clock', 'truncated',
                                      'missing-method', 'exit-order', 'incomplete', 'action', 'thread'])
def test_engineering_art_corruption_rejected(mutation):
    raw = trace_bytes()
    if mutation == 'overflow':
        raw = raw.replace(b'overflow=false', b'overflow=true')
    elif mutation == 'record-count':
        raw = raw.replace(b'num-method-calls=4', b'num-method-calls=5')
    elif mutation == 'layout':
        raw = raw.replace(b'SLOW', b'BAD!')
    elif mutation == 'clock':
        raw = raw.replace(b'clock=dual', b'clock=wall')
    elif mutation == 'truncated':
        raw = raw[:-1]
    elif mutation == 'missing-method':
        raw = raw.replace(b'\tdataChanged\t', b'\totherMethod\t')
    else:
        records = [(1, 4, 0, 1), (1, 8, 0, 2), (1, 9, 0, 3), (1, 5, 0, 4)]
        if mutation == 'exit-order':
            records[2] = (1, 5, 0, 3)
        elif mutation == 'incomplete':
            records[3] = (1, 12, 0, 4)
        elif mutation == 'action':
            records[0] = (1, 7, 0, 1)
        elif mutation == 'thread':
            records[0] = (0, 4, 0, 1)
        raw = trace_bytes(records)
    with pytest.raises(ValueError):
        parse_backup(raw)


def test_art_unnamed_and_unwind_remain_explicit():
    raw = trace_bytes([(1, 4, 0, 1), (1, 12, 0, 2), (1, 13, 0, 3), (1, 6, 0, 4)])
    result = parse_backup(raw)
    assert result['unnamed_method_count'] == 1 and result['unnamed_records'] == 2
    assert result['calls'][0]['completion'] == 'unwound'
    with pytest.raises(ValueError):
        validate_backup(raw)


def test_actual_manifest_public_scope():
    path = private_manifest()
    result = evaluate(json.loads(path.read_text()), path.parent)
    assert len(result['runs']) == 4 and len(result['matrices']) == 3
    assert result['status'] == 'bounded_evidence_verified' and result['strict_gate'] == 'BLOCKED'
    assert result['removed_keys'] == 0 and result['no_vm_verified'] is False
    assert result['original_xml_writer_replaced'] is False
    text = json.dumps(result)
    assert '/Users/' not in text and '/data/' not in text and 'bytes_hex' not in text


@pytest.mark.parametrize('mutation', ['missing-case', 'order', 'input', 'initial', 'read', 'write', 'state',
    'commit', 'window', 'xml-hash', 'xml-restore', 'restored', 'mode', 'scope', 'writer', 'no-vm',
    'backup-delivery', 'concurrency', 'bridge-error', 'bridge-retire', 'bridge-reference', 'bridge-fallback',
    'bridge-count', 'bridge-phase', 'bridge-thread', 'bridge-caller', 'bridge-receipt', 'bridge-window'])
def test_actual_matrix_mutations_rejected(mutation):
    matrix, original, final = actual_matrix()
    row = matrix['cases'][1]
    if mutation == 'missing-case':
        matrix['cases'].pop()
    elif mutation == 'order':
        matrix['cases'].reverse()
    elif mutation == 'input':
        row['initial'][0] = 'changed'
    elif mutation == 'initial':
        row['before'][0]['value'] = 'changed'
    elif mutation == 'read':
        row['native']['result']['value'] = 'changed'
    elif mutation == 'write':
        matrix['cases'][7]['native']['input'] = 'changed'
    elif mutation == 'state':
        row['after'][1]['value'] = 'changed'
    elif mutation == 'commit':
        row['events_after'] = row['events_after'].replace('commit:exit\ttrue', 'commit:exit\tfalse')
    elif mutation == 'window':
        row['events_after'] = row['events_before']
    elif mutation == 'xml-hash':
        row['xml_after'][0]['sha256'] = '0' * 64
    elif mutation == 'xml-restore':
        matrix['xml_restored'][0] = matrix['cases'][0]['xml_after'][0]
    elif mutation == 'restored':
        matrix['restored'] = False
    elif mutation == 'mode':
        matrix['native_bridge_replacement'] = False
    elif mutation == 'scope':
        matrix['scope'] = 'cold-start'
    elif mutation == 'writer':
        matrix['original_xml_writer_replaced'] = True
    elif mutation == 'no-vm':
        matrix['no_vm_verified'] = True
    elif mutation == 'backup-delivery':
        matrix['backup_delivery'] = 'verified'
    elif mutation == 'concurrency':
        matrix['concurrency_equivalence'] = 'verified'
    elif mutation == 'bridge-error':
        final['errors'] = ['failure']
    elif mutation == 'bridge-retire':
        final['retired'] = False
    elif mutation == 'bridge-reference':
        final['retained_allocations'] = 0
    elif mutation == 'bridge-fallback':
        final['fallbacks'] = 1
    elif mutation == 'bridge-count':
        final['events'].pop()
    elif mutation == 'bridge-window':
        row['bridge_before']['events'] = []
    else:
        event = row['bridge_after']['events'][-1]
        if mutation == 'bridge-phase':
            event['phase'] = 'full_trace'
        elif mutation == 'bridge-thread':
            event['thread'] += 1
        elif mutation == 'bridge-caller':
            event['caller'] = 'unknown'
        elif mutation == 'bridge-receipt':
            event['receipt']['backup_method_returns'] = 0
    with pytest.raises(ValueError):
        validate_matrix(matrix, original, final)


@pytest.mark.parametrize('run', ['original-final', 'replacement-retained-1', 'replacement-final-2'])
def test_actual_backup_methods(run):
    raw = (private_manifest().parent / run / 'java-methods.trace').read_bytes()
    result = validate_backup(raw)
    assert result['request_backup_returns'] == result['data_changed_returns'] == 5
    assert result['backup_delivery'] == 'unknown'
    corrupted = raw.replace(b'\trequestBackup\t', b'\totherMethod\t')
    with pytest.raises(ValueError):
        validate_backup(corrupted)


def test_actual_non_pn_fallbacks_preserved_without_gate_promotion():
    base = private_manifest().parent
    original = json.loads((base / 'original-final/pn-bridge-matrix.json').read_text())
    matrix = json.loads((base / 'replacement-final-2/pn-bridge-matrix.json').read_text())
    final = json.loads((base / 'replacement-final-2/pn-bridge-final.json').read_text())
    result = validate_matrix(matrix, original, final)
    assert result['non_pn_original_bridge_fallbacks'] == final['fallbacks'] == 31
    assert result['natural_battle_pn_calls_observed'] == 0
    assert result['fallback_key_names'].endswith('unknown')
    matrix['cases'][0]['bridge_before']['fallbacks'] = 0
    with pytest.raises(ValueError, match='non-PN fallback changed'):
        validate_matrix(matrix, original, final)


@pytest.mark.parametrize('mutation', ['process', 'input-identity', 'source', 'snapshot', 'resource-live',
                                      'provider-failure', 'asset-event', 'build', 'dex', 'guest-artifact',
                                      'configuration', 'bridge-install', 'incomplete', 'action'])
def test_actual_manifest_semantic_corruption_rejected(monkeypatch, mutation):
    path = private_manifest()
    manifest = json.loads(path.read_text())
    run = 'replacement-final-2/'
    relative = run + {'process': 'owned-process.json', 'input-identity': 'identity-after.json',
        'source': 'sources/e2_1_pn_bridge.js', 'snapshot': 'snapshots/tick-000100.bin',
        'resource-live': 'resource-final.json', 'provider-failure': 'resource-final.json',
        'asset-event': 'e0-final.json', 'configuration': 'private-config.json',
        'bridge-install': 'pn-bridge-install.json', 'incomplete': 'run-finished.json',
        'action': 'control-receipts.json'}.get(mutation, 'unused')
    if mutation in ['build', 'dex', 'guest-artifact']:
        relative = 'guest-artifacts-after.json' if mutation == 'guest-artifact' else 'build-reproduction.json'
    target = path.parent / relative
    raw = target.read_bytes()
    if mutation == 'source':
        changed = raw + b'\n'
    elif mutation == 'snapshot':
        changed = bytes([raw[0] ^ 1]) + raw[1:]
    else:
        value = json.loads(raw)
        if mutation == 'process':
            value = json.loads((path.parent / 'original-final/owned-process.json').read_text())
        elif mutation == 'input-identity':
            value['update_manifest_sha256'] = '0' * 64
        elif mutation in ['resource-live', 'provider-failure']:
            value['metrics']['live_assets' if mutation == 'resource-live' else 'failures'] = '1'
        elif mutation == 'asset-event':
            row = dict(value['counts'][0], labels=['AAsset_read'], count=1)
            value['counts'].append(row)
        elif mutation in ['build', 'dex']:
            value['rebuilt_sha256' if mutation == 'build' else 'rebuilt_dex_sha256'] = '0' * 64
        elif mutation == 'guest-artifact':
            value['identities'][next(iter(value['identities']))] = '0' * 64
        elif mutation == 'configuration':
            value['mode'] = 'strict-targeted'
        elif mutation == 'bridge-install':
            value['fallbacks'] = 1
        elif mutation == 'incomplete':
            value['status'] = 'failed'
        elif mutation == 'action':
            next(row for row in value if row['phase'] == 'initial_state')['response']['ok'] = False
        changed = json.dumps(value).encode()
    manifest['files'][relative] = hashlib.sha256(changed).hexdigest()
    actual_read = Path.read_bytes

    def read_attachment(candidate):
        return changed if candidate == target else actual_read(candidate)

    monkeypatch.setattr(Path, 'read_bytes', read_attachment)
    with pytest.raises(ValueError):
        evaluate(manifest, path.parent)


@pytest.mark.parametrize('relative', ['../outside', '/absolute'])
def test_attachment_escape_rejected(tmp_path, relative):
    with pytest.raises(ValueError, match='unsafe attachment path'):
        evaluate(dict(schema='e2.1h-private-manifest.v1', files={relative: '0' * 64}), tmp_path)


def test_attachment_hash_symlink_and_binding_rejected(tmp_path):
    target = tmp_path / 'fixture'
    target.write_text('engineering attachment')
    manifest = dict(schema='e2.1h-private-manifest.v1', files={'fixture': '0' * 64})
    with pytest.raises(ValueError, match='attachment hash differs'):
        evaluate(manifest, tmp_path)
    (tmp_path / 'link').symlink_to(target)
    manifest['files'] = {'link': hashlib.sha256(target.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match='symlink attachment'):
        evaluate(manifest, tmp_path)
    manifest.update(files={}, verification_sources={'E21PreferenceObserver.java': 'unbound'})
    with pytest.raises(ValueError, match='unbound attachment'):
        evaluate(manifest, tmp_path)
