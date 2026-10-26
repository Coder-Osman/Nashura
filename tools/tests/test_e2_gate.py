import copy
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from verify_e2_gate import completed_run, controls, evaluate, replacement


def test_incomplete_manifest_is_rejected():
    with pytest.raises(ValueError, match='wrong evidence schema'):
        evaluate({}, Path('.'))


def test_incomplete_lifecycle_is_rejected():
    with pytest.raises(ValueError, match='lifecycle missing'):
        controls([])


def test_summary_only_pass_is_rejected():
    with pytest.raises((KeyError, ValueError)):
        replacement({'schema': 'e2-config-replacement.v1', 'mode': 'strict-targeted',
                     'status': {'installed': True}, 'errors': []}, {}, {}, {'mode': 'strict-targeted'})


@pytest.fixture(scope='module')
def evidence():
    location = os.environ.get('E2_PRIVATE_MANIFEST')
    if not location:
        pytest.skip('private native evidence not distributed; no native pass inferred')
    path = Path(location)
    manifest = json.loads(path.read_text())
    base = path.parent
    original = json.loads((base / manifest['original'] / 'e2-final.json').read_text())
    run = manifest['runs'][-1]
    receipt = json.loads((base / run / 'replacement-final.json').read_text())
    observer = json.loads((base / run / 'e2-final.json').read_text())
    config = json.loads((base / run / 'private-config.json').read_text())
    return manifest, base, original, receipt, observer, config


def test_real_private_bundle_passes(evidence):
    manifest, base, *_ = evidence
    result = evaluate(manifest, base)
    assert result['status'] == 'blocked'
    assert result['fixed_trajectory_check'] == 'passed'
    assert result['targeted_strict_gate_verified'] is False
    assert result['no_vm_verified'] is False
    assert result['android_free_verified'] is False
    bounded = evaluate(manifest, base, scope='fixed-trajectory')
    assert bounded['status'] == 'passed'
    assert bounded['targeted_strict_gate_verified'] is False


def test_completed_receipt_does_not_override_missing_evidence(tmp_path):
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    (attempt / 'evidence-incomplete.json').write_text('{}')
    with pytest.raises(ValueError, match='final evidence incomplete'):
        completed_run(tmp_path, 'attempt', dict(status='completed', exit_code=0), [])


def test_completed_receipt_does_not_override_frida_errors(tmp_path):
    with pytest.raises(ValueError, match='Frida script error'):
        completed_run(tmp_path, 'attempt', dict(status='completed', exit_code=0),
                      [dict(payload=dict(type='error'))])


@pytest.mark.parametrize('mutation', ['hash', 'duplicate', 'omitted_mode', 'identity', 'missing_negative'])
def test_real_bundle_mutations_rejected(evidence, mutation):
    manifest, base, *_ = evidence
    changed = copy.deepcopy(manifest)
    if mutation == 'hash':
        changed['files'][changed['original'] + '/trace.json'] = '0' * 64
    elif mutation == 'duplicate':
        changed['runs'][1] = changed['runs'][0]
    elif mutation == 'omitted_mode':
        changed['runs'] = changed['runs'][:-2]
    elif mutation == 'identity':
        changed['update_manifest_sha256'] = '0' * 64
    else:
        del changed['negative_native']['omitted_key']
    with pytest.raises((ValueError, KeyError)):
        evaluate(changed, base)


@pytest.mark.parametrize('mutation', ['fallback', 'wrong_value', 'missing_key', 'write', 'route',
                                     'store_failure', 'unlisted_fallback', 'count', 'jni', 'observer', 'scope',
                                     'wrong_exclusion_source', 'relabel_target', 'missing_caller', 'store_count'])
def test_real_replacement_semantic_mutations_rejected(evidence, mutation):
    _, _, original, receipt, observer, config = evidence
    receipt, observer, config = copy.deepcopy((receipt, observer, config))
    if mutation == 'fallback':
        receipt['status']['targeted_fallback'] = 1
    elif mutation == 'wrong_value':
        next(row for row in receipt['events'] if row['route'] == 'native_read')['actual'] = 'wrong'
    elif mutation == 'missing_key':
        receipt['events'] = receipt['events'][1:]
    elif mutation == 'write':
        next(row for row in receipt['events'] if row['route'] == 'native_write')['value'] = 'wrong'
    elif mutation == 'route':
        next(row for row in receipt['events'] if row['route'] == 'native_read')['route'] = 'original_fallback'
    elif mutation == 'store_failure':
        receipt['store']['failures'] = '1'
    elif mutation == 'unlisted_fallback':
        next(row for row in receipt['events'] if row['route'] == 'original_fallback')['key'] = 'never_observed'
    elif mutation == 'count':
        receipt['status']['targeted_reads'] = 31
    elif mutation == 'jni':
        observer['config_calls'].append(dict(operation='PreferenceJNIReadBridge', key={'value': 'SfxEn'}))
    elif mutation == 'observer':
        observer['dropped']['config_calls'] = 1
    elif mutation == 'scope':
        config['replacement']['entries']['SfxEn']['value'] = 'wrong'
    elif mutation == 'wrong_exclusion_source':
        next(iter(config['replacement']['excluded_keys'].values()))['evidence'] = [-1]
    elif mutation == 'relabel_target':
        next(row for row in receipt['events'] if row['targeted'])['targeted'] = False
    elif mutation == 'missing_caller':
        del receipt['events'][0]['caller']
    else:
        receipt['store']['reads'] = '0'
    with pytest.raises((ValueError, KeyError)):
        replacement(receipt, observer, original, config)


def test_real_snapshot_byte_mutation_rejected(evidence, tmp_path):
    from verify_e0_snapshots import verify
    manifest, base, *_ = evidence
    original = base / manifest['original']
    target = tmp_path / 'snapshots'
    shutil.copytree(original / 'snapshots', target)
    path = target / 'tick-000123.bin'
    content = bytearray(path.read_bytes())
    content[-1] ^= 1
    path.write_bytes(content)
    receipt = json.loads((original / 'trace.json').read_text())
    with pytest.raises(ValueError, match='differs at tick 123'):
        verify(original / 'snapshots', target, receipt, receipt)
