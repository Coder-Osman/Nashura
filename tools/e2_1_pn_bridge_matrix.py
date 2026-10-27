"""Exercise the actual PN native entry, retaining the original per-store backend."""

import base64
import json

from e2_1_pn_matrix import CASES


def run_bridge_matrix(observer, semantics, replacement, save, capture_xml):
    result = dict(schema='e2.1h-actual-bridge-matrix.v1', cases=[], restored=False,
                  native_bridge_replacement=replacement, original_xml_writer_replaced=False,
                  scope='explicit sequential post-trajectory native PN entry; original Java per-store backend',
                  backup_delivery='unknown', concurrency_equivalence='unknown', no_vm_verified=False)
    if observer.command('pn-begin') != 'pn-captured\t2':
        raise RuntimeError('original PN capture not acknowledged')
    try:
        files = observer.command('files').strip().splitlines()
        if len(files) != 2 or len(set(files)) != 2:
            raise RuntimeError('distinct preference XML paths required')
        result['initial'] = json.loads(observer.command('pn-state'))
        result['xml_before'] = capture_xml('before', files)
        for item in CASES:
            pair = [base64.b64encode(value.encode()).decode() for value in item['initial']]
            if observer.command('pn-set:' + ':'.join(pair)) != 'pn-set\t2':
                raise RuntimeError('PN pair setup not acknowledged')
            row = dict(item)
            result['cases'].append(row)
            row['before'] = json.loads(observer.command('pn-state'))
            row['events_before'] = observer.command('snapshot')
            if replacement:
                row['bridge_before'] = observer.bridge_snapshot()
            probe = {key: value for key, value in item.items() if key != 'initial'}
            row['native'] = semantics.batch([dict(probe, key='PN_POPUP')])[0]
            if replacement:
                row['bridge_after'] = observer.bridge_snapshot()
            row['events_after'] = observer.command('snapshot')
            row['after'] = json.loads(observer.command('pn-state'))
            row['xml_after'] = capture_xml(row['case'], files)
            save('pn-bridge-matrix.json', result)
    finally:
        result['restore_receipt'] = observer.command('pn-restore')
        result['restored'] = result['restore_receipt'] == 'pn-restored\t2'
        if result['restored']:
            result['xml_restored'] = capture_xml('restored', files)
        save('pn-bridge-matrix.json', result)
    if not result['restored']:
        raise RuntimeError('original PN state not restored')
    return result
