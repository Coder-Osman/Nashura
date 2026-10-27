import copy
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1b_public_summary import summarize
from verify_e2_1b_evidence import evaluate


def fixture():
    row = dict(key='STATIC_KEY', battle_necessity='unknown', removability='unknown',
        natural_bridge_calls=1, operations=dict(PreferenceNativeReadBridge=1),
        callers=['libg.so+0x123'], phases=['cold'], threads=[1], source_sequences=[2],
        wrapper_observations=[])
    rows = [dict(row, key='KEY_' + str(index)) for index in range(46)]
    return dict(status='bounded_evidence_verified', strict_gate='BLOCKED',
        no_vm_verified=False, android_free_verified=False, training_admissible=False,
        pn_native_replacement=False, runs=[],
        removability=dict(rows=rows, natural_bridge_calls=46, explicit_probe_calls=3),
        source_apk_sha256='a' * 64, installed_dex_sha256='b' * 64,
        concurrent=dict(workers=2, write_calls=8, read_calls=8, commit_calls=0,
                        overlapping_java_root_pairs=6, trace_sha256='c' * 64))


def test_public_summary_omits_values_raw_paths_and_unknown_fields():
    evidence = fixture()
    evidence['PRIVATE_PATH'] = '/private/path/SENTINEL'
    evidence['removability']['rows'][1]['key'] = 'titan.installevent'
    evidence['removability']['rows'][0].update(value='SENTINEL', returned_string=dict(value='SENTINEL'))
    public = json.dumps(summarize(evidence))
    assert 'SENTINEL' not in public
    assert 'PRIVATE_PATH' not in public
    assert '"unknown": 46' in public
    assert '"pn_native_replacement": false' in public


@pytest.mark.parametrize('mutation', ['gate', 'vm', 'necessary', 'duplicate', 'caller', 'counter', 'digest'])
def test_exporter_rejects_promotions_and_malformed_public_fields(mutation):
    evidence = copy.deepcopy(fixture())
    if mutation == 'gate':
        evidence['strict_gate'] = 'PASS'
    elif mutation == 'vm':
        evidence['no_vm_verified'] = True
    elif mutation == 'necessary':
        evidence['removability']['rows'][0]['battle_necessity'] = 'required'
    elif mutation == 'duplicate':
        evidence['removability']['rows'][1]['key'] = 'KEY_0'
    elif mutation == 'caller':
        evidence['removability']['rows'][0]['callers'] = ['/private/value']
    elif mutation == 'counter':
        evidence['concurrent']['write_calls'] = 'PRIVATE_VALUE'
    else:
        evidence['source_apk_sha256'] = 'PRIVATE_VALUE'
    with pytest.raises(ValueError):
        summarize(evidence)


def test_real_public_export_when_private_manifest_supplied():
    path = os.environ.get('E2_1B_PRIVATE_MANIFEST')
    if not path:
        pytest.skip('requires private real E2.1-B evidence')
    target = Path(path)
    summary, ledger = summarize(evaluate(json.loads(target.read_text()), target.resolve().parent))
    assert len(summary['runs']) == 5
    assert ledger['classification_counts'] == dict(unknown=46)
    assert summary['concurrent']['overlapping_java_root_pairs'] > 0
