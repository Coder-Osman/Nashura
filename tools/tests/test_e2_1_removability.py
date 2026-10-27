import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_removability import build


def evidence():
    ledger = dict(unique_excluded_keys=2, rows=[dict(key=key, replacement_class='required') for key in ['SEEN', 'UNSEEN']])
    observer = dict(errors=[], dropped=dict(config_calls=0), config_calls=[dict(sequence=index,
        operation='PreferenceNativeReadBridge', thread=thread, phase='cold', caller='libg.so+0x123',
        key=dict(value='SEEN'), returned_string=dict(value='PRIVATE_VALUE')) for index, thread in enumerate([17, 71])])
    probes = [dict(key='SEEN', thread=71, operation='read')]
    return ledger, observer, probes


def test_no_observation_or_reachability_claims_necessity_or_removability():
    result = build(*evidence(), identities={})
    assert result['classification_counts'] == {'unknown': 2}
    assert result['natural_bridge_calls'] == result['explicit_probe_calls'] == 1
    seen, unseen = result['rows']
    assert seen['original_initialization_reachable'] is True
    assert unseen['original_initialization_reachable'] is False
    assert seen['battle_necessity'] == unseen['battle_necessity'] == 'unknown'
    assert 'PRIVATE_VALUE' not in str(result)


def test_wrapper_callers_are_separate_nonadditive_observations():
    ledger, observer, probes = evidence()
    observer['config_calls'].append(dict(sequence=3, operation='PreferenceRead',
        thread=17, phase='configuration', phase_chain=['preinit', 'configuration'],
        caller='libg.so+0x456', key=dict(value='SEEN'), returned_string=dict(value='PRIVATE_VALUE')))
    result = build(ledger, observer, probes, {})
    assert result['natural_bridge_calls'] == 1
    wrapper = result['rows'][0]['wrapper_observations'][0]
    assert wrapper['caller'] == 'libg.so+0x456'
    assert wrapper['phase_chain'] == ['preinit', 'configuration']
    assert 'PRIVATE_VALUE' not in str(result)


@pytest.mark.parametrize('mutation', ['dropped', 'error', 'probe_thread', 'probe_count', 'duplicate'])
def test_rejects_ambiguous_attribution_and_incomplete_sources(mutation):
    ledger, observer, probes = copy.deepcopy(evidence())
    if mutation == 'dropped':
        observer['dropped']['config_calls'] = 1
    elif mutation == 'error':
        observer['errors'] = ['failed']
    elif mutation == 'probe_thread':
        probes[0]['thread'] = 999
    elif mutation == 'probe_count':
        probes += probes
    else:
        ledger['rows'][1]['key'] = 'SEEN'
    with pytest.raises(ValueError):
        build(ledger, observer, probes, {})
