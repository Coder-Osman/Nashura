import pytest
from summarize_headless_benchmark import summarize


def log():
    return chr(10).join([
        'HEADLESS_BATTLE_METRICS completed=3601 wall_ns=1000 step_total_ns=900 step_min_ns=1 step_max_ns=90 p50_ns=5 p95_ns=20 p99_ns=70 peak_rss_kb=2048 trace=0 start_ns=100 end_ns=1100',
        'HEADLESS_BATTLE_MEMORY rss_kb=2048 pss_kb=1024',
        'HEADLESS_GAME_STATE_STEP_RETURN tick_before=0 tick_after=3601 completed=3601 requested=10000 finalized=1',
        'HEADLESS_BATTLE_RESET iteration=1 repeats=1 digest=6c959326dd2f09cc bytes=3091 equal=1 validated=1',
        'HEADLESS_BATTLE_READY=UNVERIFIED', 'HEADLESS_REJECTED_PM_CALLS=0',
        'HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1'])


def test_validated_statistics_and_memory_units():
    result = summarize(log(), 1)
    assert result['matches'] == 1
    assert result['peak_rss_mib'] == 2
    assert result['sampled_pss_peak_mib'] == 1
    assert result['median_match_ms'] == 0.001
    assert result['readiness'] == 'UNVERIFIED'
    assert result['memory_sample_counts'] == {'rss_kb': 1, 'pss_kb': 1}


def test_fixture_specific_terminal_identity_is_configurable():
    text = log().replace('digest=6c959326dd2f09cc bytes=3091',
                         'digest=1234567890abcdef bytes=3072')
    result = summarize(text, 1, expected_digest='1234567890abcdef', expected_bytes=3072)
    assert result['terminal_digest'] == '1234567890abcdef'
    assert result['terminal_bytes'] == 3072
    with pytest.raises(ValueError, match='serialization'):
        summarize(text, 1, expected_digest='1234567890abcdef')


@pytest.mark.parametrize('kwargs', [
    {'expected_tick': 0}, {'expected_digest': 'not-a-digest'}, {'expected_bytes': 0},
])
def test_invalid_fixture_terminal_identity_is_rejected(kwargs):
    with pytest.raises(ValueError, match='expected terminal identity'):
        summarize(log(), 1, **kwargs)


def test_unavailable_proc_memory_is_null_not_negative_memory():
    result = summarize(log().replace('rss_kb=2048 pss_kb=1024', 'rss_kb=-1 pss_kb=-1'), 1)
    assert result['sampled_rss_peak_mib'] is None
    assert result['sampled_pss_peak_mib'] is None
    assert result['memory_sample_counts'] == {'rss_kb': 0, 'pss_kb': 0}


def repeated_log(iteration, start):
    text = log()
    text = text.replace("wall_ns=1000 step_total_ns=900", "wall_ns=1000 step_total_ns=900 sample_count=226 sample_every=16 loaded_commands=52 injected_commands=0")
    text = text.replace("start_ns=100 end_ns=1100", f"start_ns={start} end_ns={start + 1000}")
    text = text.replace("iteration=1 repeats=1", f"iteration={iteration} repeats=2")
    return text


@pytest.mark.parametrize('old,new', [
    ('trace=0', 'trace=1'), ('finalized=1', 'finalized=0'),
    ('equal=1', 'equal=0'), ('validated=1', 'validated=0'),
    ('digest=6c959326dd2f09cc', 'digest=0000000000000000'),
    ('iteration=1', 'iteration=2'), ('bytes=3091', 'bytes=3090'),
    ('end_ns=1100', 'end_ns=1101'), ('p95_ns=20', 'p95_ns=100'),
    ('HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1', ''),
    ('HEADLESS_REJECTED_PM_CALLS=0', 'HEADLESS_REJECTED_PM_CALLS=1'),
])
def test_reject_invalid_results(old, new):
    with pytest.raises(ValueError):
        summarize(log().replace(old, new), 1)


def test_reject_incomplete_or_duplicate_matches():
    with pytest.raises(ValueError):
        summarize(log(), 30)
    with pytest.raises(ValueError):
        summarize(log() + chr(10) + log(), 1)


def test_minimal_sampled_benchmark_excludes_warmup_and_checks_nonempty_actions():
    text = repeated_log(1, 100) + chr(10) + repeated_log(2, 1200)
    result = summarize(text, 2, warmup_matches=1)
    assert result['schema'] == 'nashura.headless-benchmark.v2'
    assert result['warmup_matches'] == 1
    assert result['measured_matches'] == 1
    assert result['total_step_samples'] == 226
    assert result['step_sample_every_ticks'] == 16
    assert result['step_percentiles_us']['p95'] == 0.02
    assert result['median_match_ms'] == 0.001
    with pytest.raises(ValueError, match='action sequence'):
        summarize(text.replace('loaded_commands=52', 'loaded_commands=0'), 2, warmup_matches=1)


def test_minimal_sampled_benchmark_rejects_wrong_sample_count():
    text = repeated_log(1, 100) + chr(10) + repeated_log(2, 1200)
    with pytest.raises(ValueError, match='sample contract'):
        summarize(text.replace('sample_count=226', 'sample_count=225'), 2)
