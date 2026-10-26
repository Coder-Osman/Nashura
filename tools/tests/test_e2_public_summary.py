import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1]))
from summarize_e2_evidence import attempts, observation


def test_summary_omits_strings_references_and_private_rows(tmp_path):
    secret = 'private-value-never-publish'
    source = dict(config_calls=[dict(key=secret)], jni_counts=[dict(name='CallObjectMethod',
        phase='full_trace', thread=100, count=5, samples=[dict(value=secret)], phase_chain=[])],
        java_vm_counts=[], reference_counts=[], dropped={}, errors=[], unknowns=['unconfigured targets'],
        captured_strings=[secret], registered_natives=[secret])
    (tmp_path / 'e2-final.json').write_text(json.dumps(source))
    result = observation(tmp_path)
    assert secret not in json.dumps(result)
    assert result['jni_counts']['total'] == 5
    assert result['jni_counts']['by_thread_role'] == {'unknown': 5}
    assert result['reference_liveness'] == 'unknown'


def test_missing_receipt_does_not_invent_exit_code(tmp_path):
    (tmp_path / 'original-attempt').mkdir()
    result = attempts(tmp_path)[0]
    assert result['recorded_status'] == 'unknown'
    assert result['recorded_exit_code'] is None
