import copy
import json
import os
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_partial_method_trace import parse_partial
from e2_1k_path_trace import parse_path
from verify_e2_1k_natural_codec import RUNS, evaluate, validate_replacement, validate_xml


def trace_fixture(duplicate=False, conflicting=False, cipher_inside=False, opaque=False, roots=6):
    rows = ['0x4\torg.nashura.evidence.E21PreferenceObserver\tnativeCommand\t(Ljava/lang/String;)Ljava/lang/String;',
            '0x8\tjavax.crypto.Cipher\tdoFinal\t([B)[B']
    if duplicate:
        rows.append(rows[0] if not conflicting else rows[0] + 'changed')
    events = [(2, 8, 0), (2, 8, 1)]
    for unused in range(roots):
        events.append((1, 4, 0))
        if cipher_inside:
            events.extend([(1, 8, 0), (1, 8, 1)])
        if opaque:
            events.extend([(1, 12, 0), (1, 12, 1)])
        events.append((1, 4, 1))
    header = ('*version\n3\ndata-file-overflow=false\nclock=dual\nnum-method-calls=' + str(len(events))
              + '\n*threads\n1\tfixture\n2\tother\n*methods\n' + '\n'.join(rows) + '\n*end\n').encode()
    binary = struct.pack('<4sHHQH', b'SLOW', 3, 32, 0, 14).ljust(32, b'\0')
    return header + binary + b''.join(struct.pack('<HIII', thread, method | action, index, index)
        for index, (thread, method, action) in enumerate(events))


def test_engineering_duplicate_metadata_is_explicit_and_conflicts_rejected():
    raw = trace_fixture(duplicate=True)
    with pytest.raises(ValueError, match='duplicate'):
        parse_partial(raw, 'org.nashura.evidence.E21PreferenceObserver', {'nativeCommand'})
    assert parse_path(raw, True)['identical_metadata_duplicates'] == 1
    with pytest.raises(ValueError, match='conflicting'):
        parse_path(trace_fixture(duplicate=True, conflicting=True), True)


def test_engineering_other_threads_and_opaque_methods_remain_distinct():
    result = parse_path(trace_fixture(opaque=True), True)
    assert result['named_cipher_calls_inside_roots'] == 0
    assert result['global_named_counts']['javax.crypto.Cipher.doFinal([B)[B'] == 1
    assert result['unnamed_methods'][0]['records'] == 12
    assert result['unnamed_method_semantics'] == 'unknown'
    assert result['exhaustive_method_coverage'] is False
    assert parse_path(trace_fixture(cipher_inside=True), True)['named_cipher_calls_inside_roots'] == 6


@pytest.mark.parametrize('raw', [trace_fixture(roots=5), trace_fixture()[:-1],
    trace_fixture().replace(b'overflow=false', b'overflow=true')])
def test_engineering_incomplete_path_evidence_rejected(raw):
    with pytest.raises(ValueError):
        parse_path(raw, True)


@pytest.mark.parametrize('mutation', ['values', 'route', 'xml-change', 'plain-entry', 'xml-count'])
def test_engineering_xml_oracle_failures_rejected(mutation):
    before = [b'<map><string name="fixture">opaque</string></map>'] * 2
    after = before[:]
    row = dict(route='original c.a.j via retained store objects', values=['8', '8'])
    assert validate_xml(row, before, after, '8')['original_java_readback_equal']
    if mutation == 'values':
        row['values'][1] = '9'
    elif mutation == 'route':
        row['route'] = 'native-read'
    elif mutation == 'xml-change':
        after[0] += b'\n'
    elif mutation == 'plain-entry':
        before = after = [b'<map><string name="PN_POPUP">8</string></map>'] * 2
    else:
        before = after = before[:1]
    with pytest.raises(ValueError):
        validate_xml(row, before, after, '8')


def private_manifest():
    value = os.environ.get('E2_1K_PRIVATE_MANIFEST')
    if not value:
        pytest.skip('requires private complete natural PN codec evidence')
    return Path(value)


def actual_replacement():
    path = private_manifest().parent / RUNS[2]
    values = [json.loads((path / name).read_text()) for name in ['return-observer-cold.json',
        'return-observer-final.json', 'pn-bridge-install.json', 'pn-bridge-final.json', 'e2-final.json',
        'return-observer-uninstall.json', 'return-observer-release.json']]
    values.append(parse_path((path / 'pn-path.trace').read_bytes(), True))
    return values


def test_actual_complete_evidence_stays_bounded_and_redacted():
    path = private_manifest()
    result = evaluate(json.loads(path.read_text()), path.parent)
    assert result['status'] == 'bounded_evidence_verified'
    assert result['strict_gate'] == 'BLOCKED' and result['removed_keys'] == 0
    assert result['no_vm_verified'] is result['android_free_verified'] is result['training_admissible'] is False
    assert result['independent_process_readback_equal'] is True
    text = json.dumps(result)
    assert '/Users/' not in text and '/data/' not in text and 'returned_string' not in text


@pytest.mark.parametrize('mutation', ['callsite', 'return', 'thread', 'entry', 'natural-windows', 'receipt',
    'route', 'read', 'write', 'explicit-phase', 'explicit-value', 'fallback', 'retired', 'references', 'release',
    'commit', 'raw-order', 'derivations', 'transforms', 'native-operation', 'native-thread', 'native-errors',
    'native-retire', 'native-coverage', 'cipher', 'old-store', 'no-vm', 'xml-replaced', 'upper-phase', 'drop'])
def test_actual_natural_codec_corruptions_rejected(mutation):
    values = copy.deepcopy(actual_replacement())
    initial, final, installed, bridge, observed, uninstall, release, path = values
    event, native = bridge['events'][0], bridge['native_backend']
    if mutation == 'callsite':
        event['caller'] = 'unknown'
    elif mutation == 'return':
        final['cold']['windows'][0]['completion'] = 'assumed'
    elif mutation == 'thread':
        event['thread'] += 1
    elif mutation == 'entry':
        final['cold']['windows'][0]['entry'] = final['cold']['windows'][1]['entry'] + 1
    elif mutation == 'natural-windows':
        final['cold']['windows'].pop()
    elif mutation == 'receipt':
        bridge['events'][1]['receipt']['changed'] = 0
    elif mutation == 'route':
        event['route'] = 'original-fallback'
    elif mutation == 'read':
        event['result'] = 'different'
    elif mutation == 'write':
        bridge['events'][1]['input'] = 'different'
    elif mutation == 'explicit-phase':
        bridge['events'][-1]['phase'] = 'configuration'
    elif mutation == 'explicit-value':
        bridge['events'][-1]['result'] = 'different'
    elif mutation == 'fallback':
        bridge['fallbacks'] += 1
    elif mutation == 'retired':
        bridge['retired'] = False
    elif mutation == 'references':
        final['references']['deleted_globals'] = 0
    elif mutation == 'release':
        release['result'] = 'unknown'
    elif mutation in {'commit', 'raw-order'}:
        window = final['cold']['windows'][1]
        window['after'] = window['after'].replace('commit:exit\ttrue', 'commit:exit\tfalse') if mutation == 'commit' else window['before']
    elif mutation == 'derivations':
        native['derivations'] += 1
    elif mutation == 'transforms':
        native['transforms'] += 1
    elif mutation == 'native-operation':
        native['events'][3]['operation'] = 'read'
    elif mutation == 'native-thread':
        native['events'][3]['thread'] += 1
    elif mutation == 'native-errors':
        native['errors'].append('failed')
    elif mutation == 'native-retire':
        native['retired'] = False
    elif mutation == 'native-coverage':
        native['unsupported'] = []
    elif mutation == 'cipher':
        path['named_cipher_calls_inside_roots'] = 1
    elif mutation == 'old-store':
        path['named_old_store_calls_inside_roots'] = 1
    elif mutation == 'no-vm':
        bridge['no_vm_verified'] = True
    elif mutation == 'xml-replaced':
        bridge['original_xml_writer_replaced'] = True
    elif mutation == 'upper-phase':
        next(row for row in observed['config_calls'] if row['operation'] == 'PreferenceRead'
             and row.get('key', {}).get('value') == 'PN_POPUP')['phase'] = 'state_create'
    else:
        observed['dropped']['config_calls'] = 1
    with pytest.raises((ValueError, KeyError)):
        validate_replacement(*values)
