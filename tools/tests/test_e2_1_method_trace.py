import os
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_method_trace import parse
from run_e2_observed import traced_probe


def fixture(events=None):
    if events is None:
        events = [(4, 0), (8, 0), (8, 1), (4, 1)]
    header = ('*version\n3\ndata-file-overflow=false\nclock=dual\nnum-method-calls=' + str(len(events))
              + '\n*threads\n1\tfixture\n*methods\n'
              + '0\tfixture\tzero\t()V\n'
              + '0x4\tcom.supercell.titan.ApplicationUtilBase\tstoreKeyValue\t(Ljava/lang/String;Ljava/lang/String;)V\n'
              + '0x8\tandroid.app.SharedPreferencesImpl$EditorImpl\tcommit\t()Z\n*end\n')
    binary = struct.pack('<4sHHQH', b'SLOW', 3, 32, 0, 14).ljust(32, b'\0')
    return header.encode() + binary + b''.join(struct.pack('<HIII', 1, method | action, index, index)
                                              for index, (method, action) in enumerate(events))


def test_trace_preserves_order_without_inventing_return_values():
    result = parse(fixture())
    assert result['roots'][0]['completion'] == 'returned'
    assert result['roots'][0]['selected_calls'][1]['method'].endswith('commit()Z')
    assert result['return_values'].startswith('unknown')
    assert result['no_vm_verified'] is False


@pytest.mark.parametrize('raw', [fixture()[:-1], fixture().replace(b'overflow=false', b'overflow=true'),
    fixture().replace(b'num-method-calls=4', b'num-method-calls=5'),
    fixture([(4, 0), (8, 1)]), fixture([(4, 0)]), fixture([(4, 0), (12, 0)]),
    fixture([(4, 0), (8, 3)]), fixture([(4, 0), (4, 0)])])
def test_trace_rejects_incomplete_or_ambiguous_evidence(raw):
    with pytest.raises(ValueError):
        parse(raw)


def test_trace_records_exception_unwind():
    assert parse(fixture([(4, 0), (4, 2)]))['roots'][0]['completion'] == 'unwound'


def test_real_trace_when_explicitly_supplied():
    path = os.environ.get('E2_1B_ART_TRACE')
    if not path:
        pytest.skip('requires private real ART trace')
    result = parse(Path(path).read_bytes())
    assert len(result['roots']) == 7
    commits = [sum(call['method'].endswith('.commit()Z') for call in row['selected_calls'])
               for row in result['roots']]
    assert commits == [2, 0, 0, 2, 0, 2, 0]
    assert all(row['completion'] == 'returned' for row in result['roots'])


@pytest.mark.parametrize('operation_error,stop_error', [(False, False), (True, False), (True, True), (False, True)])
def test_trace_always_stops_and_retains_primary_failure(operation_error, stop_error):
    calls = []
    class Oracle:
        def start(self):
            calls.append('start')

        def stop(self):
            calls.append('stop')
            if stop_error:
                raise RuntimeError('stop failed')

    def operation():
        calls.append('operation')
        if operation_error:
            raise ValueError('probe failed')
        return 17

    def save(name, value):
        calls.append((name, value))

    if operation_error or stop_error:
        with pytest.raises(ValueError if operation_error else RuntimeError):
            traced_probe(operation, Oracle(), lambda: calls.append('collect'), save)
    else:
        assert traced_probe(operation, Oracle(), lambda: calls.append('collect'), save) == 17
    assert calls[:3] == ['start', 'operation', 'stop']
    if stop_error:
        assert calls[3][0] == 'java-trace-cleanup-failure.json'
        assert calls[3][1]['original_error'] == ('probe failed' if operation_error else None)
    else:
        assert calls[3] == 'collect'
