import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_public_summary import digest, summaries
from test_e2_1_gate import attachment, native_evidence
from verify_e2_1_gate import evaluate


def test_value_digest_contains_no_plaintext():
    result = digest('PRIVATE_VALUE_中文')
    assert result['bytes'] == len('PRIVATE_VALUE_中文'.encode())
    assert 'PRIVATE_VALUE' not in json.dumps(result)


def test_summary_rejects_unverified_gate():
    with pytest.raises(ValueError, match='identity-verified'):
        summaries({}, Path('.'), dict(status='passed'))


def test_summary_rejects_unbound_manifest():
    with pytest.raises(ValueError, match='identity differs'):
        summaries(dict(files={}), Path('.'), dict(bounded_native_comparisons='passed',
                                                status='blocked', attachment_sha256={'other': '0' * 64}))


def test_real_summary_preserves_limits_and_redacts_values(native_evidence):
    manifest, base = native_evidence
    gate = evaluate(manifest, base)
    result = summaries(manifest, base, gate)
    output = json.dumps(result)
    assert str(base) not in output and 'attachment_sha256' not in output
    assert not result['e2_1_gate.json']['no_vm_verified']
    assert result['e2_1_gate.json']['required_unreplaced_count'] == 46
    for row in result['e2_1_semantics_matrix.json']['rows']:
        assert 'input' not in row and 'value' not in row and 'result' not in row
    config = attachment(native_evidence, 'matrix-4-replacement/private-config.json')
    for entry in config['replacement']['entries'].values():
        if len(entry['value']) > 16:
            assert entry['value'] not in output
