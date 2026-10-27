"""Private original-codec / host-native / guest-native differential after the match."""

import base64
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from e2_1_pn_codec import PNCodec


TEXTS = ['', 'a', 'b' * 15, 'c' * 16, 'd' * 17, '中文', '🙂', 'line\n<>&"', 'x' * 255, 'z' * 4095]


def run_codec_matrix(observer, native, config, save, capture_xml):
    profile_path = Path(config['profile'])
    profile = json.loads(profile_path.read_text())
    keys = [bytes.fromhex(value) for value in profile['keys']]
    iv = bytes.fromhex(profile['iv'])
    if len(keys) != 2 or any(len(key) != 32 for key in keys) or len(iv) != 16:
        raise ValueError('explicit private two-store crypto profile required')
    host = PNCodec(config['host_library'], config['host_crypto_library'])
    rows = []
    malformed = []
    result = dict(schema='e2.1-pn-codec-matrix.v1', cases=rows, malformed=malformed,
        profile_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        scope='post-trajectory original c.a.h/j plus SharedPreferences XML; explicit-key codec only',
        original_bridge_replaced=False, original_xml_writer_replaced=False, restored=False,
        backup_delivery='unknown', no_vm_verified=False)
    captured = False
    files = None
    try:
        if observer.command('pn-begin') != 'pn-captured\t2':
            raise RuntimeError('original PN capture not acknowledged')
        captured = True
        result['original_profile'] = json.loads(observer.command('codec-info'))
        files = observer.command('files').strip().splitlines()
        if len(files) != 2 or len(set(files)) != 2:
            raise RuntimeError('distinct original preference XML paths required')
        save('pn-codec-original-xml-before.json', capture_xml('before', files))
        for store, key in enumerate(keys):
            encoded_key = host.encode('PN_POPUP', key)
            if observer.command(f'codec-encode:{store}:key:' + base64.b64encode(b'PN_POPUP').decode()) != encoded_key:
                raise RuntimeError('derived profile does not match original PN key')
            for ordinal, text in enumerate(TEXTS):
                row = dict(store=store, ordinal=ordinal, utf8_bytes=len(text.encode()), checks=[])
                rows.append(row)
                for purpose in ('key', 'value'):
                    vector = dict(key=list(key), iv=list(iv) if purpose == 'value' else [],
                        input=list(text.encode()), mode=int(purpose == 'value'), encrypt=1)
                    observed = observer.command(f'codec-encode:{store}:{purpose}:' + base64.b64encode(text.encode()).decode())
                    original_bytes = base64.b64decode(observed, validate=True)
                    guest = native.transform(vector)
                    host_bytes = host.transform(text.encode(), key, iv if purpose == 'value' else None)
                    if guest['status'] != 0 or bytes(guest['output']) != original_bytes or host_bytes != original_bytes:
                        raise RuntimeError('original/guest/host codec bytes differ')
                    reverse = native.transform(dict(vector, input=list(original_bytes), encrypt=0))
                    if reverse['status'] != 0 or bytes(reverse['output']) != text.encode() or host.transform(
                            original_bytes, key, iv if purpose == 'value' else None, encrypt=False) != text.encode():
                        raise RuntimeError('native codec inverse differs')
                    row['checks'].append(dict(purpose=purpose, encoded=observed, guest=guest, reverse=reverse,
                        host_sha256=hashlib.sha256(host_bytes).hexdigest()))
                    if purpose == 'value':
                        injected = base64.b64encode(bytes(guest['output'])).decode()
                        row['injected_source'] = 'guest-native-codec'
                        setup = observer.command(f'codec-raw:{store}:' + base64.b64encode(injected.encode()).decode())
                        if setup != 'codec-raw-set':
                            raise RuntimeError('raw PN setup not acknowledged')
                        row['original_read_b64'] = observer.command(f'codec-read:{store}')
                        if base64.b64decode(row['original_read_b64'], validate=True) != text.encode():
                            raise RuntimeError('original store rejected compatible native ciphertext')
                        xml = capture_xml(f's{store}-t{ordinal}', files)
                        row['xml'] = xml
                        state = json.loads(observer.command(f'codec-state:{store}'))
                        row['original_state'] = state
                        entries = {child.attrib['name']: child.text or '' for child in ET.fromstring(bytes.fromhex(xml[store]['bytes_hex']))}
                        expected = observed if text else None
                        if entries.get(encoded_key) != expected or state.get('encoded') != expected:
                            raise RuntimeError('actual original XML/raw PN mismatch')
                save('pn-codec-matrix.json', result)
            for name, encoded in [('bad-base64', '%%%'), ('short-block', base64.b64encode(b'bad').decode()),
                                   ('zero-block', base64.b64encode(bytes(16)).decode())]:
                if observer.command(f'codec-raw:{store}:' + base64.b64encode(encoded.encode()).decode()) != 'codec-raw-set':
                    raise RuntimeError('malformed PN setup not acknowledged')
                row = dict(store=store, case=name, encoded=encoded,
                    events_before=observer.command('snapshot'))
                malformed.append(row)
                row['original_read_b64'] = observer.command(f'codec-read:{store}')
                row['events_after'] = observer.command('snapshot')
                row['after'] = json.loads(observer.command(f'codec-state:{store}'))
                row['xml'] = capture_xml(f's{store}-{name}', files)
                if row['original_read_b64'] != '' or row['after'].get('encoded') is not None:
                    raise RuntimeError('original malformed-entry cleanup differs from static expectation')
                save('pn-codec-matrix.json', result)
    finally:
        cleanup_errors = []
        for name, operation in [('host_close', host.close), ('native_close', native.close)]:
            try:
                result[name] = operation()
            except Exception as failure:
                cleanup_errors.append(dict(operation=name, error=str(failure)))
        try:
            if captured:
                result['restore_receipt'] = observer.command('pn-restore')
                result['restored'] = result['restore_receipt'] == 'pn-restored\t2'
                if files is not None:
                    result['restored_xml'] = capture_xml('restored', files)
        except Exception as failure:
            cleanup_errors.append(dict(operation='pn_restore', error=str(failure)))
        result['cleanup_errors'] = cleanup_errors
        save('pn-codec-matrix.json', result)
    if not result['restored']:
        raise RuntimeError('original PN raw entries not restored')
    if result['cleanup_errors']:
        raise RuntimeError('codec cleanup failed; retained evidence required')
    return result
