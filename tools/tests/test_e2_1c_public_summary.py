import copy
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1c_public_summary import summarize
from verify_e2_1c_evidence import evaluate


def fixture():
    coverage = dict(exhaustive=False, failure_unnamed_methods=6, race_unnamed_methods=6,
                    failure_unnamed_records=28, race_unnamed_records=200)
    return dict(status='bounded_evidence_verified', strict_gate='BLOCKED',
                no_vm_verified=False, android_free_verified=False, training_admissible=False,
                tenfold_speedup_verified=False, pn_native_replacement=False, removed_keys=0,
                art_coverage=coverage, attachment_count=1, retained_failed_attempts=1,
                runs=[dict(run='baseline', observed=False, snapshot_count=3602, terminal_tick=3601,
                           injected_actions=28, total_bytes=9120440, trajectory_sha256='0' * 64)],
                sequential=[dict(case='same_write', operation='write', commits={})],
                failure=dict(cases=[dict(case='disk_denied_write', operation='write',
                                        commits={'o:false': 1, 'p:false': 1})]),
                race=dict(workers=2, writes=8, reads=8, overlapping_art_root_pairs=6,
                          own_reads=7, other_reads=1, unknown_reads=0, commits={'o:true': 7, 'p:true': 6}))


def test_summary_ignores_private_fields_at_every_level():
    evidence = fixture()
    expected = summarize(evidence)
    for row in [evidence, evidence['runs'][0], evidence['failure'], evidence['race'], evidence['art_coverage']]:
        row['private_value'] = 'PRIVATE_SENTINEL'
    actual = summarize(evidence)
    assert actual == expected
    assert 'PRIVATE_SENTINEL' not in json.dumps(actual)


@pytest.mark.parametrize('field', ['no_vm_verified', 'android_free_verified', 'training_admissible',
                                  'tenfold_speedup_verified', 'pn_native_replacement'])
def test_gate_promotion_rejected(field):
    evidence = fixture()
    evidence[field] = True
    with pytest.raises(ValueError):
        summarize(evidence)


@pytest.mark.parametrize('mutation', ['count', 'label', 'digest', 'commit-label', 'boolean', 'exhaustive', 'removed'])
def test_unsafe_public_fields_rejected(mutation):
    evidence = fixture()
    if mutation == 'count':
        evidence['race']['writes'] = 'PRIVATE_SENTINEL'
    elif mutation == 'label':
        evidence['runs'][0]['run'] = '/private/path'
    elif mutation == 'digest':
        evidence['runs'][0]['trajectory_sha256'] = 'PRIVATE_SENTINEL'
    elif mutation == 'commit-label':
        evidence['race']['commits'] = {'private': 1}
    elif mutation == 'boolean':
        evidence['runs'][0]['observed'] = 'PRIVATE_SENTINEL'
    elif mutation == 'exhaustive':
        evidence['art_coverage']['exhaustive'] = True
    else:
        evidence['removed_keys'] = 1
    with pytest.raises(ValueError):
        summarize(evidence)


def test_real_private_evidence_export_is_bounded():
    manifest_path = os.environ.get('E2_1C_PRIVATE_MANIFEST')
    if not manifest_path:
        pytest.skip('requires real private native evidence')
    target = Path(manifest_path)
    original = evaluate(json.loads(target.read_text()), target.resolve().parent)
    evidence = copy.deepcopy(original)
    evidence['private_path'] = 'PRIVATE_SENTINEL'
    result = summarize(evidence)
    assert len(result['runs']) == 6
    assert result['failure']['cases'][0]['commits'] == {'o:false': 1, 'p:false': 1}
    assert result['race']['other_reads'] > 0
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)
    assert result['strict_gate'] == 'BLOCKED'
