"""Bounded original-bridge PN priority/backfill comparison after the native match."""

import base64
import json


CASES = [
    dict(case='both-empty', initial=['', ''], operation='read'),
    dict(case='o-backfill', initial=['matrix-o', ''], operation='read'),
    dict(case='p-only', initial=['', 'matrix-p'], operation='read'),
    dict(case='conflict', initial=['matrix-o', 'matrix-p'], operation='read'),
    dict(case='equal', initial=['matrix-same', 'matrix-same'], operation='read'),
    dict(case='unicode-backfill', initial=['中文', ''], operation='read'),
    dict(case='unicode-conflict', initial=['旧', '新'], operation='read'),
    dict(case='write-conflict', initial=['matrix-o', 'matrix-p'], operation='write', value='matrix-new'),
    dict(case='write-partial', initial=['matrix-new', 'matrix-p'], operation='write', value='matrix-new'),
    dict(case='write-same', initial=['matrix-same', 'matrix-same'], operation='write', value='matrix-same'),
    dict(case='write-empty', initial=['matrix-o', 'matrix-p'], operation='write', value=''),
]


def run_matrix(observer, semantics, candidate, save):
    rows = []
    result = dict(schema='e2.1-pn-original-matrix.v1', cases=rows, restored=False,
                  scope='explicit post-trajectory original native bridge; independent logical-state candidate',
                  backup_delivery='unknown', native_bridge_replacement=False, no_vm_verified=False)
    receipt = observer.command('pn-begin')
    if receipt != 'pn-captured\t2':
        raise RuntimeError('original PN capture not acknowledged')
    save('pn-matrix-capture.json', dict(result=receipt))
    try:
        for item in CASES:
            pair = [base64.b64encode(value.encode()).decode() for value in item['initial']]
            if observer.command('pn-set:' + ':'.join(pair)) != 'pn-set\t2':
                raise RuntimeError('PN pair setup not acknowledged')
            row = dict(item)
            rows.append(row)
            row['before'] = json.loads(observer.command('pn-state'))
            row['events_before'] = observer.command('snapshot')
            probe = {key: value for key, value in item.items() if key != 'initial'}
            probe['key'] = 'PN_POPUP'
            row['original'] = semantics.batch([probe])[0]
            row['events_after'] = observer.command('snapshot')
            row['after'] = json.loads(observer.command('pn-state'))
            row['candidate'] = candidate.run(item)
            save('pn-matrix.json', result)
    finally:
        result['restore_receipt'] = observer.command('pn-restore')
        result['restored'] = result['restore_receipt'] == 'pn-restored\t2'
        save('pn-matrix.json', result)
    if not result['restored']:
        raise RuntimeError('original raw PN entries not restored')
    return result
