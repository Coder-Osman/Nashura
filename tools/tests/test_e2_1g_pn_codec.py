import hashlib
import json
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_pn_codec import PNCodec
from verify_e2_1g_pn_codec import evaluate, validate_matrix, xml_entries


def private_manifest():
    value = os.environ.get('E2_1G_PRIVATE_MANIFEST')
    if not value:
        pytest.skip('requires actual original Cipher/XML evidence')
    return Path(value)


def actual_matrix(run='codec-scoped-1'):
    path = private_manifest()
    base = path.parent
    manifest = json.loads(path.read_text())
    directory = base / run
    matrix = json.loads((directory / 'pn-codec-matrix.json').read_text())
    matrix['original_xml_before'] = json.loads((directory / 'pn-codec-original-xml-before.json').read_text())
    final = json.loads((directory / 'pn-codec-final.json').read_text())
    config = json.loads((directory / 'private-config.json').read_text())
    profile = json.loads((base / manifest['profile']).read_text())
    host = PNCodec(base / manifest['artifacts']['host']['path'], config['pn_codec']['host_crypto_library'])
    return matrix, final, profile, host


def test_actual_manifest_and_public_scope():
    path = private_manifest()
    result = evaluate(json.loads(path.read_text()), path.parent)
    assert result['status'] == 'bounded_evidence_verified' and len(result['runs']) == 3
    assert result['strict_gate'] == 'BLOCKED' and result['removed_keys'] == 0
    assert result['pn_native_replacement'] is result['no_vm_verified'] is False
    text = json.dumps(result)
    assert '/Users/' not in text and '/data/' not in text
    profile = json.loads((path.parent / json.loads(path.read_text())['profile']).read_text())
    assert all(secret not in text for secret in profile['keys'] + [profile['iv']])


@pytest.mark.parametrize('run', ['codec-scoped-1', 'codec-scoped-2'])
def test_actual_cipher_xml_matrix(run):
    matrix, final, profile, host = actual_matrix(run)
    try:
        result = validate_matrix(matrix, final, profile, host)
        assert result['cipher_comparisons'] == 40 and result['native_transforms'] == 80
        assert result['malformed_cleanup_cases'] == 6 and result['xml_restored'] is True
    finally:
        host.close()


@pytest.mark.parametrize('mutation', ['empty', 'order', 'text-size', 'ciphertext', 'guest', 'reverse',
    'host-hash', 'readback', 'source', 'state', 'mode', 'missing-malformed', 'malformed-read',
    'malformed-state', 'commit', 'window', 'restore', 'receipt', 'cleanup', 'final-calls',
    'final-closed', 'algorithm', 'charset', 'iv', 'profile', 'xml-hash', 'xml-restore',
    'promotion', 'writer-promotion', 'backup', 'no-vm'])
def test_actual_evidence_mutations_rejected(mutation):
    matrix, final, profile, host = actual_matrix()
    row = matrix['cases'][1]
    if mutation == 'empty':
        matrix['cases'] = []
    elif mutation == 'order':
        matrix['cases'].reverse()
    elif mutation == 'text-size':
        row['utf8_bytes'] += 1
    elif mutation == 'ciphertext':
        row['checks'][0]['encoded'] = 'AAAA'
    elif mutation == 'guest':
        row['checks'][0]['guest']['status'] = -4
    elif mutation == 'reverse':
        row['checks'][0]['reverse']['output'] = []
    elif mutation == 'host-hash':
        row['checks'][0]['host_sha256'] = '0' * 64
    elif mutation == 'readback':
        row['original_read_b64'] = ''
    elif mutation == 'source':
        row['injected_source'] = 'original'
    elif mutation == 'state':
        row['original_state'] = {}
    elif mutation == 'mode':
        row['checks'].reverse()
    elif mutation == 'missing-malformed':
        matrix['malformed'].pop()
    elif mutation == 'malformed-read':
        matrix['malformed'][0]['original_read_b64'] = 'YQ=='
    elif mutation == 'malformed-state':
        matrix['malformed'][0]['after']['encoded'] = 'retained'
    elif mutation == 'commit':
        matrix['malformed'][0]['events_after'] = matrix['malformed'][0]['events_after'].replace(
            'commit:exit\ttrue', 'commit:exit\tfalse')
    elif mutation == 'window':
        matrix['malformed'][0]['events_after'] = matrix['malformed'][0]['events_before']
    elif mutation == 'restore':
        matrix['restored'] = False
    elif mutation == 'receipt':
        matrix['restore_receipt'] = 'pn-restored\t1'
    elif mutation == 'cleanup':
        matrix['cleanup_errors'] = [{'operation': 'native_close', 'error': 'test'}]
    elif mutation == 'final-calls':
        final['calls'] -= 1
    elif mutation == 'final-closed':
        final['closed'] = False
    elif mutation == 'algorithm':
        matrix['original_profile'][0]['a']['algorithm'] = 'unknown'
    elif mutation == 'charset':
        matrix['original_profile'][0]['charset'] = 'unknown'
    elif mutation == 'iv':
        matrix['original_profile'][0]['a']['iv'] = ''
    elif mutation == 'profile':
        profile['keys'][0] = '00' * 32
    elif mutation == 'xml-hash':
        row['xml'][0]['sha256'] = '0' * 64
    elif mutation == 'xml-restore':
        matrix['restored_xml'][0] = record(b'<map><string name="unexpected">changed</string></map>')
    elif mutation == 'promotion':
        matrix['original_bridge_replaced'] = True
    elif mutation == 'writer-promotion':
        matrix['original_xml_writer_replaced'] = True
    elif mutation == 'backup':
        matrix['backup_delivery'] = 'verified'
    elif mutation == 'no-vm':
        matrix['no_vm_verified'] = True
    try:
        with pytest.raises(ValueError):
            validate_matrix(matrix, final, profile, host)
    finally:
        host.close()


def record(raw):
    return dict(sha256=hashlib.sha256(raw).hexdigest(), bytes_hex=raw.hex())


@pytest.mark.parametrize('store', [0, 1])
def test_actual_unrelated_preference_change_rejected(store):
    matrix, final, profile, host = actual_matrix()
    row = matrix['cases'][1]
    root = ET.fromstring(bytes.fromhex(row['xml'][store]['bytes_hex']))
    ET.SubElement(root, 'string', name='e21g-unrelated-mutation').text = 'changed'
    row['xml'][store] = record(ET.tostring(root))
    try:
        with pytest.raises(ValueError, match='unrelated preference changed'):
            validate_matrix(matrix, final, profile, host)
    finally:
        host.close()


@pytest.mark.parametrize('raw', [b'<map><string name="x"/><int name="x" value="1"/></map>',
    b'<map><string/></map>', b'<wrong/>', b'<!DOCTYPE map><map/>',
    b'<!DOCTYPE map [<!ENTITY example "value">]><map/>'])
def test_ambiguous_xml_rejected(raw):
    with pytest.raises(ValueError):
        xml_entries(record(raw))


def test_xml_order_independent_but_value_sensitive():
    first = b'<map><string name="x"> </string><int name="y" value="1"/></map>'
    second = b'<map>\n<int value="1" name="y"/>\n<string name="x"> </string>\n</map>'
    assert xml_entries(record(first)) == xml_entries(record(second))
    assert xml_entries(record(first)) != xml_entries(record(second.replace(b'> </string>', b'></string>')))


@pytest.mark.parametrize('relative', ['../outside', '/absolute'])
def test_attachment_escape_rejected(tmp_path, relative):
    with pytest.raises(ValueError, match='unsafe attachment path'):
        evaluate(dict(schema='e2.1g-private-manifest.v1', files={relative: '0' * 64}), tmp_path)


def test_attachment_hash_symlink_and_missing_binding_rejected(tmp_path):
    target = tmp_path / 'fixture'
    target.write_text('engineering attachment')
    manifest = dict(schema='e2.1g-private-manifest.v1', files={'fixture': '0' * 64})
    with pytest.raises(ValueError, match='attachment hash differs'):
        evaluate(manifest, tmp_path)
    (tmp_path / 'link').symlink_to(target)
    manifest['files'] = {'link': hashlib.sha256(target.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match='symlink attachment'):
        evaluate(manifest, tmp_path)
    manifest.update(files={}, verification_sources={'E21PreferenceObserver.java': 'unbound'})
    with pytest.raises(ValueError, match='unbound attachment'):
        evaluate(manifest, tmp_path)
