import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import e2_1_pn_derive_matrix as matrix_module
from verify_e2_1j_pn_derive import derivation_profile, evaluate, validate_matrix


class FixtureCodec:
    def __init__(self, *arguments):
        self.closed = False

    def encode(self, text, key, iv=None):
        return base64.b64encode(key + (iv or b'') + text.encode()).decode()

    def close(self):
        self.closed = True


def fixture():
    source = dict(source='original-app-context Settings.Secure', identity='0123456789abcdef', package='fixture.pkg')
    material = dict(table=list(range(24)), iv_source=bytes(range(35)).hex())
    profile = derivation_profile(source, material)
    codec = FixtureCodec()
    xml = [b'<fixture-a/>', b'<fixture-b/>']
    matrix = dict(schema='e2.1j-derivation-matrix.v1', source=source, profile=profile,
        guest=dict(status=0, keys=list(b''.join(bytes.fromhex(key) for key in profile['keys'])),
                   iv=list(bytes.fromhex(profile['iv']))), initialization_replaced=False,
        original_xml_writer_replaced=False, no_vm_verified=False,
        source_acquisition='Java retained; explicit transfer to native after complete trajectory',
        cipher_info=[dict(store=store, charset='UTF-8', **{name: dict(iv=base64.b64encode(
            bytes.fromhex(profile['iv'])).decode()) for name in ['a', 'b']}) for store in range(2)],
        restored=True, restore_receipt='pn-restored\t2', cleanup_errors=[],
        xml_before=[dict(sha256=hashlib.sha256(raw).hexdigest(), bytes_hex=raw.hex()) for raw in xml],
        cases=[], negatives=[dict(case=case, result=dict(status=-1, keys=[165] * 64, iv=[165] * 16))
            for case in ['empty-identity', 'non-hex-identity', 'short-table', 'short-iv', 'invalid-package']])
    matrix['xml_after'] = copy.deepcopy(matrix['xml_before'])
    for store in range(2):
        for ordinal, text in enumerate(matrix_module.TEXTS):
            for purpose in ['key', 'value']:
                result = codec.encode(text, bytes.fromhex(profile['keys'][store]),
                                      bytes.fromhex(profile['iv']) if purpose == 'value' else None)
                matrix['cases'].append(dict(store=store, ordinal=ordinal, purpose=purpose, original=result, native=result))
    native = dict(schema='e2.1j-native-derivation.v1', calls=6, initialization_replaced=False,
                  original_xml_writer_replaced=False, no_vm_verified=False)
    return matrix, native, material, dict(package='fixture.pkg'), codec, xml, copy.deepcopy(xml)


def test_fixture_checks_are_engineering_only():
    assert validate_matrix(*fixture()) == dict(original_cipher_cases=40, native_negative_cases=5,
        host_guest_hashlib_equal=True, original_xml_bytes_unchanged=True, stores=2)


@pytest.mark.parametrize('mutation', ['source', 'identity', 'package', 'profile', 'guest', 'iv', 'charset',
    'store', 'missing-case', 'order', 'cipher', 'negative', 'partial-output', 'cleanup', 'restore',
    'xml', 'xml-receipt', 'calls', 'promotion'])
def test_fixture_rejects_incomplete_or_forged_matrix(mutation):
    values = fixture()
    matrix, native = values[:2]
    if mutation == 'source': matrix['source']['source'] = 'shell android_id'
    elif mutation == 'identity': matrix['source']['identity'] = 'unknown'
    elif mutation == 'package': matrix['source']['package'] = 'other.pkg'
    elif mutation == 'profile': matrix['profile']['keys'][0] = '00' * 32
    elif mutation == 'guest': matrix['guest']['keys'][0] ^= 1
    elif mutation == 'iv': matrix['cipher_info'][0]['a']['iv'] = base64.b64encode(bytes(16)).decode()
    elif mutation == 'charset': matrix['cipher_info'][0]['charset'] = 'UTF-16'
    elif mutation == 'store': matrix['cipher_info'][0]['store'] = 1
    elif mutation == 'missing-case': matrix['cases'].pop()
    elif mutation == 'order': matrix['cases'].reverse()
    elif mutation == 'cipher': matrix['cases'][0]['native'] = matrix['cases'][0]['original'] = 'forged'
    elif mutation == 'negative': matrix['negatives'].pop()
    elif mutation == 'partial-output': matrix['negatives'][0]['result']['keys'][0] = 0
    elif mutation == 'cleanup': matrix['cleanup_errors'] = [dict(operation='pn_restore', error='fixture')]
    elif mutation == 'restore': matrix['restored'] = False
    elif mutation == 'xml': values[-1][0] = b'changed'
    elif mutation == 'xml-receipt': matrix['xml_after'][0]['bytes_hex'] = '00'
    elif mutation == 'calls': native['calls'] = 5
    elif mutation == 'promotion': matrix['no_vm_verified'] = True
    with pytest.raises(ValueError):
        validate_matrix(*values)


@pytest.mark.parametrize('failure', ['cipher', 'codec-close', 'restore', 'xml-after'])
def test_matrix_preserves_cleanup_failure_evidence(tmp_path, monkeypatch, failure):
    matrix, _, material, config, _, _, _ = fixture()
    path = tmp_path / 'material.json'
    path.write_text(json.dumps(material))
    config.update(material=str(path), host_library='fixture', host_crypto_library='fixture', host_codec_library='fixture')
    receipts, commands = {}, []

    class Derive:
        def __init__(self, *arguments): pass
        def derive(self, *arguments): return matrix['profile']

    class Codec(FixtureCodec):
        def encode(self, *arguments):
            if failure == 'cipher': raise RuntimeError('fixture cipher failure')
            return super().encode(*arguments)
        def close(self):
            if failure == 'codec-close': raise RuntimeError('fixture close failure')
            super().close()

    class Observer:
        def command(self, text):
            commands.append(text)
            if text == 'derivation-source': return json.dumps(matrix['source'])
            if text == 'pn-begin': return 'pn-captured\t2'
            if text == 'codec-info': return json.dumps(matrix['cipher_info'])
            if text == 'files': return '/fixture/a\n/fixture/b'
            if text == 'pn-restore':
                if failure == 'restore': raise RuntimeError('fixture restore failure')
                return 'pn-restored\t2'
            _, store, purpose, encoded = text.split(':', 3)
            return FixtureCodec().encode(base64.b64decode(encoded).decode(),
                bytes.fromhex(matrix['profile']['keys'][int(store)]),
                bytes.fromhex(matrix['profile']['iv']) if purpose == 'value' else None)

    class Native:
        def run(self, vector):
            if len(vector['identity']) == 16 and vector['identity'] != list(b'X' * 16) and len(vector['table']) == 24 and len(vector['iv_source']) >= 16 and b'/' not in bytes(vector['package']):
                return matrix['guest']
            return dict(status=-1, keys=[165] * 64, iv=[165] * 16)

    def capture(label, files):
        if label == 'after' and failure == 'xml-after': raise RuntimeError('fixture XML failure')
        return matrix['xml_before']

    monkeypatch.setattr(matrix_module, 'PNDerivation', Derive)
    monkeypatch.setattr(matrix_module, 'PNCodec', Codec)
    def save(name, value): receipts[name] = copy.deepcopy(value)
    with pytest.raises(RuntimeError):
        matrix_module.run_derivation_matrix(Observer(), Native(), config, save, capture)
    final = receipts['pn-derivation-matrix.json']
    assert 'pn-restore' in commands
    assert final['restored'] is (failure != 'restore')
    assert bool(final['cleanup_errors']) is (failure != 'cipher')


def private_manifest():
    path = os.environ.get('E2_1J_PRIVATE_MANIFEST')
    if not path:
        pytest.skip('requires actual private native derivation attachments')
    return Path(path)


def test_actual_native_evidence_remains_bounded():
    path = private_manifest()
    result = evaluate(json.loads(path.read_text()), path.parent)
    assert result['status'] == 'bounded_evidence_verified'
    assert result['strict_gate'] == 'BLOCKED' and result['removed_keys'] == 0
    assert all(result[key] is False for key in ['no_vm_verified', 'android_free_verified',
        'training_admissible', 'tenfold_speedup_verified', 'initialization_replaced', 'original_xml_writer_replaced'])
    encoded = json.dumps(result)
    assert '/Users/' not in encoded and '/data/' not in encoded and 'identity' not in result


@pytest.mark.parametrize('mutation', ['traversal', 'absolute', 'wrong-sha', 'missing-source', 'missing-snapshot'])
def test_actual_attachment_gate_rejects_mutations(mutation):
    path = private_manifest()
    manifest = json.loads(path.read_text())
    if mutation in ['traversal', 'absolute']:
        manifest['files'][('../' if mutation == 'traversal' else '/') + 'fixture'] = '0' * 64
    elif mutation == 'wrong-sha': manifest['files']['private-material.json'] = '0' * 64
    elif mutation == 'missing-source': manifest['files'].pop(manifest['verification_sources']['e2_1_pn_derive.c'])
    else: manifest['files'].pop('derivation-final-2/snapshots/tick-000001.bin')
    with pytest.raises(ValueError):
        evaluate(manifest, path.parent)


@pytest.mark.parametrize('parent_link', [False, True])
def test_attachment_gate_rejects_symlink(tmp_path, parent_link):
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'fixture'
    target.write_bytes(b'fixture')
    base = tmp_path / 'evidence'
    base.mkdir()
    link = base / 'linked'
    link.symlink_to(outside if parent_link else target, target_is_directory=parent_link)
    relative = 'linked/fixture' if parent_link else 'linked'
    manifest = dict(schema='e2.1j-private-manifest.v1',
        runs=['baseline-final', 'derivation-final-1', 'derivation-final-2'],
        files={relative: hashlib.sha256(target.read_bytes()).hexdigest()})
    with pytest.raises(ValueError, match='unsafe attachment path'):
        evaluate(manifest, base)


def test_explicit_derivation_rpc_gate():
    source = Path(__file__).parents[1] / 'e2_1_return_observer.js'
    program = """
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
for (const enabled of [false, true]) {
  const context = {rpc:{exports:{}}}; vm.createContext(context);
  vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
  context.invoke = operation => operation;
  context.rpc.exports.configure({jni_attach:0x17d8ae8,
    dex_path:'/data/user/0/nullsroyale.rel.free/fixture.dex',
    optimized_directory:'/data/user/0/nullsroyale.rel.free/code_cache',
    enable_pn_derivation:enabled, enable_pn_codec:enabled});
  for (const command of ['derivation-source', 'codec-info', 'codec-encode:0:key:']) {
    if (enabled) assert.equal(context.rpc.exports.command(command), command);
    else assert.throws(() => context.rpc.exports.command(command), /invalid command/);
  }
  assert.throws(() => context.rpc.exports.command('derivation-source:extra'), /invalid command/);
}
"""
    subprocess.run(['node', '-e', program, str(source)], check=True)
