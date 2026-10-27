"""Explicit raw PN fixtures through the real native bridge, after the full match."""

import base64
import json


CASES = [
    ('missing-both', ('missing', 'missing'), 'read', None),
    ('missing-preferred', ('value', 'missing'), 'read', None),
    ('missing-older', ('missing', 'value'), 'read', None),
    ('legacy-both', ('legacy', 'legacy'), 'read', None),
    ('legacy-backfill', ('legacy', 'missing'), 'read', None),
    ('plain-empty', ('plain-empty', 'plain-empty'), 'read', None),
    ('legacy-cipher-conflict', ('conflict', 'conflict'), 'read', None),
    ('legacy-empty-cipher', ('legacy-empty-cipher', 'legacy-empty-cipher'), 'read', None),
    ('empty-raw', ('empty-raw', 'empty-raw'), 'read', None),
    ('empty-cipher', ('empty-cipher', 'empty-cipher'), 'read', None),
    ('bad-base64', ('bad-base64', 'bad-base64'), 'read', None),
    ('short-block', ('short-block', 'short-block'), 'read', None),
    ('bad-padding', ('bad-padding', 'bad-padding'), 'read', None),
    ('malformed-preferred', ('value', 'bad-base64'), 'read', None),
    ('legacy-malformed', ('legacy-malformed', 'legacy-malformed'), 'read', None),
    ('write-empty', ('value', 'value'), 'write', ''),
    ('write-empty-missing', ('missing', 'missing'), 'write', ''),
    ('write-over-legacy', ('legacy', 'legacy'), 'write', 'new-value'),
    ('write-over-malformed', ('bad-base64', 'bad-base64'), 'write', 'new-value'),
]


def encoded(value):
    return base64.b64encode(value.encode()).decode()


def raw_fixture(kind, store, cipher):
    text = ('older-value', 'preferred-value')[store]
    if kind == 'value':
        return dict(plain=None, encoded=cipher(text))
    if kind == 'conflict':
        return dict(plain='legacy-' + text, encoded=cipher(text))
    if kind == 'empty-cipher':
        return dict(plain=None, encoded=cipher(''))
    fixtures = {
        'missing': (None, None),
        'legacy': (text, None),
        'plain-empty': ('', None),
        'legacy-empty-cipher': (text, ''),
        'empty-raw': (None, ''),
        'bad-base64': (None, '%%%'),
        'short-block': (None, base64.b64encode(b'bad').decode()),
        'bad-padding': (None, base64.b64encode(bytes(16)).decode()),
        'legacy-malformed': (text, '%%%'),
    }
    plain, ciphertext = fixtures[kind]
    return dict(plain=plain, encoded=ciphertext)


def run_edge_matrix(observer, semantics, replacement, save, capture_xml, case_names=None,
                    cipher_state=False, direct_cipher=False, stream=None):
    if stream is not None and (replacement or not direct_cipher):
        raise ValueError('stream is a separate original-Cipher diagnostic only')
    cases = CASES
    if case_names is not None:
        by_name = {item[0]: item for item in CASES}
        if not isinstance(case_names, list) or not case_names or len(case_names) != len(set(case_names)) or any(
                name not in by_name for name in case_names):
            raise ValueError('explicit unique known PN cases required')
        cases = [by_name[name] for name in case_names]
    result = dict(schema='e2.1l-pn-edge-matrix.v1', cases=[], restored=False,
                  replacement=replacement, scope='explicit post-trajectory raw fixtures; actual native PN entry',
                  concurrency_equivalence='unknown', no_vm_verified=False, strict_gate='BLOCKED')
    result['case_order'] = [item[0] for item in cases]
    if observer.command('pn-begin') != 'pn-captured\t2':
        raise RuntimeError('original PN capture not acknowledged')
    files = None
    try:
        files = observer.command('files').strip().splitlines()
        if len(files) != 2 or len(set(files)) != 2:
            raise RuntimeError('distinct original XML files required')
        result['xml_before'] = capture_xml('before', files)
        if direct_cipher:
            result['direct_cipher'] = []
            for store in range(2):
                valid = observer.command(f'codec-encode:{store}:value:' + encoded('diagnostic-value'))
                inputs = [('valid-before', valid), ('short-block', base64.b64encode(b'bad').decode()),
                          ('valid-after-short', valid), ('valid-after-short-again', valid)] if store == 0 else [
                              ('valid-before', valid), ('bad-padding', base64.b64encode(bytes(16)).decode()),
                              ('valid-after-padding', valid), ('valid-after-padding-again', valid)]
                for name, ciphertext in inputs:
                    row = dict(case=name, store=store, input=ciphertext,
                               result=json.loads(observer.command(f'codec-decrypt:{store}:' + ciphertext)))
                    result['direct_cipher'].append(row)
                    if stream is not None:
                        row['stream'] = stream.decode(store, list(base64.b64decode(ciphertext, validate=True)))
            save('pn-edge-matrix.json', result)
        for name, kinds, operation, value in cases:
            row = dict(case=name, kinds=kinds, operation=operation)
            result['cases'].append(row)
            fixtures = []
            for store, kind in enumerate(kinds):
                cipher = lambda text: observer.command(f'codec-encode:{store}:value:' + encoded(text))
                fixture = raw_fixture(kind, store, cipher)
                fixtures.append(fixture)
                setup = observer.command(f'codec-fixture:{store}:' + encoded(json.dumps(fixture)))
                if setup != 'codec-fixture-set':
                    raise RuntimeError('raw PN setup not acknowledged')
            row['fixtures'] = fixtures
            row['events_before'] = observer.command('snapshot')
            if cipher_state:
                row['cipher_before'] = json.loads(observer.command('codec-runtime'))
            if replacement:
                row['bridge_before'] = observer.bridge_snapshot()
            item = dict(case=name, operation=operation, key='PN_POPUP')
            if operation == 'write':
                item['value'] = value
            row['native'] = semantics.batch([item])[0]
            row['events_after'] = observer.command('snapshot')
            if cipher_state:
                row['cipher_after'] = json.loads(observer.command('codec-runtime'))
            if replacement:
                row['bridge_after'] = observer.bridge_snapshot()
            row['raw_after'] = [json.loads(observer.command(f'codec-inspect:{store}')) for store in range(2)]
            row['xml_after'] = capture_xml(name, files)
            save('pn-edge-matrix.json', result)
    finally:
        if stream is not None:
            try:
                result['stream_close'] = stream.close()
            except Exception as failure:
                result['stream_close_error'] = type(failure).__name__
        result['restore_receipt'] = observer.command('pn-restore')
        result['restored'] = result['restore_receipt'] == 'pn-restored\t2'
        if files is not None and result['restored']:
            result['xml_restored'] = capture_xml('restored', files)
        save('pn-edge-matrix.json', result)
    if not result['restored']:
        raise RuntimeError('original PN entries not restored')
    return result
