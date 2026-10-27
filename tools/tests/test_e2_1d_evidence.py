import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1d_public_summary import summarize
from verify_e2_1d_evidence import evaluate, restart_readback, validate_cold


def private_base():
    target = os.environ.get('E2_1D_PRIVATE_MANIFEST')
    if not target:
        pytest.skip('requires actual private native cold-start evidence')
    return Path(target)


def read(base, name):
    return json.loads((base / name).read_text())


def cold_inputs():
    base = private_base().parent / 'cold-3'
    return [read(base, name) for name in ['return-observer-cold.json', 'return-observer-final.json',
        'e2-final.json', 'return-observer-uninstall.json', 'return-observer-release.json']]


def test_real_manifest_and_allowlisted_publication():
    path = private_base()
    result = evaluate(read(path.parent, path.name), path.parent)
    assert len(result['runs']) == 3 and result['restart_readback_equal'] is True
    expected = summarize(result)
    result['private'] = 'PRIVATE_SENTINEL'
    result['runs'][1]['cold']['private'] = 'PRIVATE_SENTINEL'
    result['runs'][1]['private'] = 'PRIVATE_SENTINEL'
    assert summarize(result) == expected
    assert 'PRIVATE_SENTINEL' not in json.dumps(expected)
    assert expected['strict_gate'] == 'BLOCKED' and expected['pn_native_replacement'] is False


@pytest.mark.parametrize('mutation', ['after', 'empty', 'caller', 'return-site', 'commit', 'errors',
    'reference', 'release', 'provenance', 'prefix', 'changed-final', 'independent-drop'])
def test_actual_cold_evidence_rejects_incomplete_or_inconsistent_inputs(mutation):
    initial, final, observed, uninstall, release = cold_inputs()
    if mutation == 'after':
        initial['cold']['windows'][0].pop('after')
        final['cold']['windows'][0].pop('after')
    elif mutation == 'empty':
        initial['cold']['windows'] = final['cold']['windows'] = []
    elif mutation == 'caller':
        for item in [initial, final]:
            item['cold']['windows'][0]['caller'] = '0x139419c'
    elif mutation == 'return-site':
        for item in [initial, final]:
            item['cold']['windows'][1]['return_site'] = '0x1001'
    elif mutation == 'commit':
        for item in [initial, final]:
            window = item['cold']['windows'][1]
            window['after'] = window['after'].replace('commit:exit\ttrue', 'commit:exit\tfalse')
    elif mutation == 'errors':
        final['cold']['errors'] = [{'stage': 'entry', 'error': 'fixture failure'}]
    elif mutation == 'reference':
        final['references']['deleted_globals'] = 0
    elif mutation == 'release':
        release['result'] = 'released\t0'
    elif mutation == 'provenance':
        for row in observed['config_calls']:
            if row['operation'] == 'PreferenceNativeReadBridge' and row.get('caller') == 'libg.so+0x1394198':
                row['thread'] += 1
    elif mutation == 'prefix':
        for item in [initial, final]:
            window = item['cold']['windows'][0]
            window['before'] = window['after']
    elif mutation == 'changed-final':
        final['cold']['windows'][0]['entry'] += 1
    else:
        observed['dropped']['config_calls'] = 1
    with pytest.raises(ValueError):
        validate_cold(initial, final, observed, uninstall, release)


@pytest.mark.parametrize('mutation', ['readback', 'process'])
def test_actual_restart_link_rejects_value_or_process_mismatch(mutation):
    base = private_base().parent
    terminal, observed, cleanup, owned = [read(base, name) for name in [
        'cold-3/semantics-terminal-reset.json', 'cold-4/e2-final.json',
        'cold-3/cleanup.json', 'cold-3/owned-process.json']]
    if mutation == 'readback':
        terminal[0]['result']['value'] = 'PRIVATE_SENTINEL'
    else:
        cleanup['pid'] += 1
    with pytest.raises(ValueError):
        restart_readback(terminal, observed, cleanup, owned)


@pytest.mark.parametrize('relative', ['../outside', '/absolute'])
def test_attachment_path_escape_rejected(tmp_path, relative):
    with pytest.raises(ValueError, match='unsafe attachment path'):
        evaluate(dict(schema='e2.1d-private-manifest.v1', files={relative: '0' * 64}), tmp_path)


def test_attachment_hash_and_symlink_rejected(tmp_path):
    target = tmp_path / 'attachment'
    target.write_text('engineering fixture')
    manifest = dict(schema='e2.1d-private-manifest.v1', files={'attachment': '0' * 64})
    with pytest.raises(ValueError, match='attachment hash differs'):
        evaluate(manifest, tmp_path)
    (tmp_path / 'link').symlink_to(target)
    manifest['files'] = {'link': '0' * 64}
    with pytest.raises(ValueError, match='symlink attachment'):
        evaluate(manifest, tmp_path)


@pytest.mark.parametrize('field', ['no_vm_verified', 'android_free_verified', 'training_admissible',
                                  'tenfold_speedup_verified', 'pn_native_replacement'])
def test_publication_rejects_unsupported_promotion(field):
    evidence = dict(status='bounded_evidence_verified', strict_gate='BLOCKED',
                    no_vm_verified=False, android_free_verified=False, training_admissible=False,
                    tenfold_speedup_verified=False, pn_native_replacement=False)
    evidence[field] = True
    with pytest.raises(ValueError, match='unsupported gate promotion'):
        summarize(evidence)
