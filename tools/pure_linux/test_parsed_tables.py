import pytest

from parsed_tables import observations


def sample():
    return '\n'.join(['PARSED_TABLE_SUMMARY\tbefore\t0\t0',
                      'PARSED_TABLE\tafter\t1\t2\t4\t2\t3\t5',
                      'PARSED_TABLE\tafter\t2\t0\t0\t0\t-1\t-1',
                      'PARSED_TABLE_SUMMARY\tafter\t2\t2'])


def test_structure_is_diagnostic_and_deterministic():
    result = observations(sample())
    assert result['complete'] and not result['catalog_ready_pass']
    assert result['summaries'] == {'before': [0, 0], 'after': [2, 2]}
    assert result['layout_sha256'] == observations('unrelated\n' + sample())['layout_sha256']


@pytest.mark.parametrize('damage', [
    lambda text: text.replace('before\t0\t0', 'before\t1\t0'),
    lambda text: text.replace('after\t1\t2\t4\t2', 'after\t1\t2\t4\t1'),
    lambda text: text.replace('after\t1\t2\t4\t2', 'after\t1\t2\t1\t2'),
    lambda text: text.replace('after\t2\t0\t0\t0', 'after\t1\t0\t0\t0'),
    lambda text: text.replace('after\t2\t0\t0\t0', 'after\t219\t0\t0\t0'),
    lambda text: text.replace('\t-1\t-1', '\t-1\t3'),
    lambda text: text.replace('SUMMARY\tafter\t2\t2', 'SUMMARY\tafter\t2\t3'),
    lambda text: '\n'.join(text.splitlines()[:-1]),
    lambda text: text + '\nPARSED_TABLE_SUMMARY\tafter\t2\t2',
    lambda text: text + '\nPARSED_TABLE\tunknown',
    lambda text: text.replace('after\t1\t2', 'after\t1\tinvalid'),
])
def test_partial_duplicate_or_inconsistent_logs_fail_closed(damage):
    assert not observations(damage(sample()))['complete']
