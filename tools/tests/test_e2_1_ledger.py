import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_exclusion_ledger import build


def evidence():
    original = dict(errors=[], dropped=dict(config_calls=0), config_calls=[dict(
        key=dict(status='decoded', value='KEY'), sequence=17, operation='PreferenceNativeReadBridge',
        phase='configuration', thread=71, caller='libg.so+0x123',
        returned_string=dict(status='decoded', value='PRIVATE_VALUE'))])
    replacement = dict(status=dict(fallback=1), events=[dict(
        key='KEY', route='original_fallback', operation='read', thread=72,
        caller='libg.so+0x123', phase='cold', native_scope=1)])
    contract = dict(excluded_keys=dict(KEY=dict(evidence=[17], reason='unproven semantics')))
    return original, replacement, contract


def test_ledger_keeps_unknown_and_redacts_values():
    result = build(*evidence(), identities={})
    assert 'PRIVATE_VALUE' not in str(result)
    assert result['fallback_events'][0]['return_summary'].startswith('unknown')
    assert result['rows'][0]['replacement_class'] == 'required'
    assert result['rows'][0]['stateful'] == 'unknown'
    assert result['unique_excluded_keys'] == result['fallback_events_count'] == 1


@pytest.mark.parametrize('mutation', ['counter', 'key', 'sequence', 'dropped'])
def test_ledger_rejects_inconsistent_sources(mutation):
    original, replacement, contract = copy.deepcopy(evidence())
    if mutation == 'counter':
        replacement['status']['fallback'] = 2
    elif mutation == 'key':
        replacement['events'][0]['key'] = 'OTHER'
    elif mutation == 'sequence':
        contract['excluded_keys']['KEY']['evidence'] = [18]
    else:
        original['dropped']['config_calls'] = 1
    with pytest.raises(ValueError):
        build(original, replacement, contract, {})
