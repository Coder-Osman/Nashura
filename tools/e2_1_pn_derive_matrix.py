"""Bounded native derivation/original Cipher differential, with private artifacts only."""

import base64
import hashlib
import json
from pathlib import Path

from e2_1_pn_codec import PNCodec
from e2_1_pn_codec_matrix import TEXTS
from e2_1_pn_derive import PNDerivation


def run_derivation_matrix(observer, native, config, save, capture_xml):
    material_path = Path(config['material'])
    material = json.loads(material_path.read_text())
    source = json.loads(observer.command('derivation-source'))
    if source['source'] != 'original-app-context Settings.Secure' or source['package'] != config['package']:
        raise ValueError('original application identity source differs')
    identity, package = source['identity'].encode('ascii'), source['package'].encode('ascii')
    table, iv_source = material['table'], bytes.fromhex(material['iv_source'])
    host = PNDerivation(config['host_library'], config['host_crypto_library'])
    profile = host.derive(identity, package, table, iv_source)
    vector = dict(identity=list(identity), package=list(package), table=table, iv_source=list(iv_source))
    actual = native.run(vector)
    expected_keys = b''.join(bytes.fromhex(key) for key in profile['keys'])
    if actual != dict(status=0, keys=list(expected_keys), iv=list(bytes.fromhex(profile['iv']))):
        raise ValueError('host and guest native derivation differ')
    result = dict(schema='e2.1j-derivation-matrix.v1', source=source, profile=profile, guest=actual,
        material_sha256=hashlib.sha256(material_path.read_bytes()).hexdigest(), cases=[], negatives=[],
        initialization_replaced=False, original_xml_writer_replaced=False, no_vm_verified=False,
        source_acquisition='Java retained; explicit transfer to native after complete trajectory', restored=False)
    save('pn-derivation-matrix.json', result)
    codec = PNCodec(config['host_codec_library'], config['host_crypto_library'])
    captured = False
    files = None
    try:
        if observer.command('pn-begin') != 'pn-captured\t2':
            raise RuntimeError('PN capture not acknowledged')
        captured = True
        result['cipher_info'] = json.loads(observer.command('codec-info'))
        if len(result['cipher_info']) != 2:
            raise ValueError('two original Cipher stores required')
        for store, info in enumerate(result['cipher_info']):
            if info['store'] != store or info['charset'].upper() != 'UTF-8' or any(
                    base64.b64decode(info[name]['iv'], validate=True) != bytes.fromhex(profile['iv'])
                    for name in ['a', 'b']):
                raise ValueError('original Cipher IV or charset differs')
        files = observer.command('files').strip().splitlines()
        if len(files) != 2 or len(set(files)) != 2:
            raise ValueError('two distinct original preference XML files required')
        result['xml_before'] = capture_xml('before', files)
        for store, encoded in enumerate(profile['keys']):
            for ordinal, text in enumerate(TEXTS):
                for purpose in ['key', 'value']:
                    observed = observer.command(f'codec-encode:{store}:{purpose}:' + base64.b64encode(text.encode()).decode())
                    expected = codec.encode(text, bytes.fromhex(encoded), bytes.fromhex(profile['iv']) if purpose == 'value' else None)
                    result['cases'].append(dict(store=store, ordinal=ordinal, purpose=purpose,
                        original=observed, native=expected))
                    save('pn-derivation-matrix.json', result)
                    if observed != expected:
                        raise ValueError('derived profile rejected by original Cipher')
        for label, changed in [
                ('empty-identity', dict(identity=[])), ('non-hex-identity', dict(identity=list(b'X' * 16))),
                ('short-table', dict(table=table[:-1])), ('short-iv', dict(iv_source=list(iv_source[:15]))),
                ('invalid-package', dict(package=list(b'invalid/package')))]:
            negative = native.run(dict(vector, **changed))
            result['negatives'].append(dict(case=label, result=negative))
            save('pn-derivation-matrix.json', result)
            if negative != dict(status=-1, keys=[165] * 64, iv=[165] * 16):
                raise ValueError('invalid source was accepted or partial key published')
    finally:
        errors = []
        try:
            codec.close()
        except Exception as failure:
            errors.append(dict(operation='codec_close', error=str(failure)))
        try:
            if captured:
                result['restore_receipt'] = observer.command('pn-restore')
                result['restored'] = result['restore_receipt'] == 'pn-restored\t2'
            if files is not None:
                result['xml_after'] = capture_xml('after', files)
        except Exception as failure:
            errors.append(dict(operation='pn_restore', error=str(failure)))
        result['cleanup_errors'] = errors
        save('pn-derivation-matrix.json', result)
    if not result['restored'] or errors:
        raise RuntimeError('PN capture not restored')
    if result['xml_before'] != result['xml_after']:
        raise RuntimeError('original preference XML changed during read-only derivation matrix')
    return result
