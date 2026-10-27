import copy
import json
import os
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_commit_events import HEADER, commit_counts, delta, parse_events, sequential, validate_case
from verify_e2_1c_evidence import evaluate
from e2_1_method_trace import parse
from e2_1_partial_method_trace import parse_partial


def fixture(result='true'):
    rows = [(1, 7, 1, 'o', 0, 'install', 'android.app.SharedPreferencesImpl'),
            (2, 7, 2, 'p', 0, 'install', 'android.app.SharedPreferencesImpl'),
            (3, 7, 3, 'o', 1, 'commit:enter', 'forward'),
            (4, 7, 4, 'o', 1, 'commit:exit', result)]
    return HEADER + '\n' + ''.join('\t'.join(map(str, row)) + '\n' for row in rows) + 'dropped\t0\n'


@pytest.mark.parametrize('value', ['true', 'false'])
def test_true_and_false_are_delegated_values(value):
    assert commit_counts(parse_events(fixture(value))) == {'o:' + value: 1}


@pytest.mark.parametrize('mutation', ['header', 'drop', 'sequence', 'clock', 'thread', 'store', 'result',
                                    'throw', 'incomplete', 'no-enter', 'editor', 'uninstall', 'unsupported'])
def test_rejects_ambiguous_or_incomplete_events(mutation):
    text = fixture()
    if mutation == 'header':
        text = text.replace('sequence', 'id', 1)
    elif mutation == 'drop':
        text = text.replace('dropped\t0', 'dropped\t1')
    elif mutation == 'sequence':
        text = text.replace('4\t7\t4', '5\t7\t4')
    elif mutation == 'clock':
        text = text.replace('4\t7\t4', '4\t7\t2')
    elif mutation == 'thread':
        text = text.replace('4\t7\t4', '4\t8\t4')
    elif mutation == 'store':
        text = text.replace('4\t7\t4\to', '4\t7\t4\tp')
    elif mutation == 'result':
        text = text.replace('exit\ttrue', 'exit\tunknown')
    elif mutation == 'throw':
        text = text.replace('exit\ttrue', 'throw\tjava.lang.Exception')
    elif mutation == 'incomplete':
        text = '\n'.join(text.splitlines()[:-2] + ['dropped\t0'])
    elif mutation == 'no-enter':
        text = text.replace('commit:enter\tforward', 'commit:exit\ttrue')
    elif mutation == 'editor':
        text = text.replace('4\t7\t4\to\t1', '4\t7\t4\to\t2')
    elif mutation == 'uninstall':
        text = text.replace('commit:exit\ttrue', 'uninstall\trestored')
    else:
        text = text.replace('commit:', 'apply:')
    with pytest.raises(ValueError):
        parse_events(text)


def test_snapshot_prefix_is_immutable_and_not_counted_twice():
    first = fixture()
    assert delta(first, first) == []
    with pytest.raises(ValueError):
        delta(first, fixture('false'))


@pytest.mark.parametrize('mutation', [None, 'key', 'exception', 'raw-status', 'count'])
def test_case_never_uses_raw_write_register_as_error(mutation):
    after = fixture()
    before = '\n'.join(after.splitlines()[:3]) + '\ndropped\t0\n'
    native = dict(case='write', operation='write', key='PN_POPUP',
                  exception_state=dict(before=False, after=False),
                  return_type='void; raw register not interpreted as status')
    expected = {'o:true': 1}
    if mutation == 'key':
        native['key'] = 'other'
    elif mutation == 'exception':
        native['exception_state']['after'] = True
    elif mutation == 'raw-status':
        native['return_type'] = 'inferred integer'
    elif mutation == 'count':
        expected = {'o:true': 2}
    row = dict(case='write', native=native, before=before, after=after)
    if mutation is None:
        assert validate_case(row, expected)['commits'] == expected
    else:
        with pytest.raises(ValueError):
            validate_case(row, expected)


def opaque_trace(encoded=None):
    encoded = [4, 12, 8, 9, 13, 5] if encoded is None else encoded
    header = ('*version\n3\ndata-file-overflow=false\nclock=dual\nnum-method-calls='
              + str(len(encoded)) + '\n*threads\n1\tfixture\n*methods\n'
              '0x4\tcom.supercell.titan.ApplicationUtilBase\tstoreKeyValue\t()V\n'
              '0x8\tandroid.app.SharedPreferencesImpl$EditorImpl\tcommit\t()Z\n'
              '0x10\tcom.supercell.titan.ApplicationUtilBase\tgetKeyValue\t()V\n*end\n')
    binary = struct.pack('<4sHHQH', b'SLOW', 3, 32, 0, 14).ljust(32, b'\0')
    return header.encode() + binary + b''.join(struct.pack('<HIII', 1, value, index, index)
                                               for index, value in enumerate(encoded))


def test_partial_art_retains_unknown_and_never_relaxes_original_parser():
    raw = opaque_trace()
    with pytest.raises(ValueError, match='unknown trace method'):
        parse(raw)
    result = parse_partial(raw)
    assert result['exhaustive_method_coverage'] is False
    assert result['unnamed_methods'] == [dict(identifier=12, entries=1, exits=1, records=2, inside_known_root=2)]
    assert result['roots'][0]['unnamed_records'] == 2
    assert len(result['roots'][0]['selected_calls']) == 2
    assert result['roots'][0]['completion'] == 'returned'


@pytest.mark.parametrize('mutation', ['stack', 'action', 'overflow', 'truncated', 'incomplete', 'missing-root',
                                    'exit-without-entry'])
def test_partial_art_still_rejects_structural_gaps(mutation):
    raw = opaque_trace()
    if mutation == 'stack':
        raw = opaque_trace([4, 12, 8, 13, 9, 5])
    elif mutation == 'action':
        raw = opaque_trace([4, 12, 15, 5])
    elif mutation == 'overflow':
        raw = raw.replace(b'overflow=false', b'overflow=true')
    elif mutation == 'truncated':
        raw = raw[:-1]
    elif mutation == 'incomplete':
        raw = opaque_trace([4, 12])
    elif mutation == 'exit-without-entry':
        raw = opaque_trace([5])
    else:
        raw = raw.replace(b'getKeyValue', b'unknownName')
    with pytest.raises(ValueError):
        parse_partial(raw)


@pytest.fixture(scope='module')
def native_manifest():
    path = os.environ.get('E2_1C_PRIVATE_MANIFEST')
    if not path:
        pytest.skip('requires real private native commit evidence')
    target = Path(path)
    return json.loads(target.read_text()), target.resolve().parent


def test_complete_real_commit_evidence(native_manifest):
    manifest, base = native_manifest
    result = evaluate(manifest, base)
    assert result['failure']['cases'][0]['commits'] == {'o:false': 1, 'p:false': 1}
    assert result['race']['overlapping_art_root_pairs'] > 0
    assert result['strict_gate'] == 'BLOCKED'
    assert result['no_vm_verified'] is result['training_admissible'] is False


@pytest.mark.parametrize('mutation', ['hash', 'escape', 'unbound', 'duplicate', 'current-source'])
def test_real_manifest_rejects_tampering(native_manifest, mutation):
    original, base = native_manifest
    manifest = copy.deepcopy(original)
    if mutation == 'hash':
        manifest['files'][manifest['baseline'] + '/trace.json'] = '0' * 64
    elif mutation == 'escape':
        manifest['files']['../other'] = '0' * 64
    elif mutation == 'unbound':
        manifest['files'].pop(manifest['baseline'] + '/trace.json')
    elif mutation == 'duplicate':
        manifest['runs'][-1] = manifest['runs'][0]
    else:
        manifest['current_observer_run'] = manifest['baseline']
    with pytest.raises((ValueError, FileNotFoundError)):
        evaluate(manifest, base)


def test_real_sequence_rejects_forged_returns_and_same_value_commits(native_manifest):
    manifest, base = native_manifest
    rows = json.loads((base / manifest['sequential_probe'] / 'results.json').read_text())
    sequential(rows)
    rows[2]['after'] = rows[2]['after'].replace('commit:exit\ttrue', 'commit:exit\tfalse')
    with pytest.raises(ValueError):
        sequential(rows)
