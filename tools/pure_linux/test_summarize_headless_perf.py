"""Synthetic accounting cases; these tests are not native battle evidence."""
from copy import deepcopy

import pytest

from run_headless_match import ENGINE_SHA256
from summarize_headless_perf import ENGINE_PATH, sample_records, summarize
from test_summarize_headless_benchmark import repeated_log


def sample(time, comm='Mainloop', pid=20, frames=None):
    if frames is None:
        frames = [(0x100000 + 0xf34c60, ENGINE_PATH),
                  (0x100000 + 0xf34e4f, ENGINE_PATH)]
    return f'{comm} {pid}/{pid} {time}: cpu-clock:u: \n' + ''.join(
        f'\t{ip:x} [unknown] ({path})\n' for ip, path in frames) + '\n'


@pytest.fixture
def inputs():
    log = repeated_log(1, 1_000_000_000) + '\n' + repeated_log(2, 2_000_000_000)
    log += '\nHEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1\n'
    script = (sample('0.900000000') + sample('1.000000100') + sample('2.000000000')
              + sample('2.000000500', frames=[(0x300, '/lib/libc.so'),
                                            (0x100000 + 0xf34e4f, ENGINE_PATH)])
              + sample('2.000001000') + sample('2.100000000'))
    mappings = ('main 20 0.100000000: PERF_RECORD_MMAP2 20/20: '
                f'[0x100000(0x2000000) @ 0 00:1e 338 3313453382]: r-xp {ENGINE_PATH}\n')
    evlist = 'cpu-clock:u: type: 1, use_clockid: 1, clockid: 1\n'
    report = '# Total Lost Samples: 0\n'
    plan = dict(benchmark_repeats=2, snapshot_trace=False,
                identities={'/closure' + ENGINE_PATH: ENGINE_SHA256},
                environment=dict(NASHURA_RESET_REPEATS='2', NASHURA_BENCHMARK_MINIMAL='1',
                                 NASHURA_QUIET='1', NASHURA_QUIET_SNAPSHOTS='1',
                                 NASHURA_BENCHMARK_VALIDATE='1'))
    return dict(log=log, script=script, mappings=mappings, evlist=evlist, report=report,
                plan=plan, exits=[0, 0, 0], repeats=2, warmup_matches=1, minimum_samples=2)


def test_windows_are_half_open_and_warmup_reset_startup_are_excluded(inputs):
    result = summarize(**inputs)
    assert result['raw_sample_count'] == 6
    assert result['sample_regions'] == {'outside_step_windows': 3, 'warmup_step_windows': 1,
                                        'measured_step_windows': 2}
    assert result['measured_samples_by_match'] == [2]
    assert result['measured_thread_samples'] == {'Mainloop': 2}
    assert result['measured_leaf_dso_samples'] == {'libg.so': 1, 'libc.so': 1}
    assert result['mainloop_libg_leaf_samples'] == 1
    assert result['spatial_query_leaf_samples'] == 1
    assert result['spatial_query_callchain_samples'] == 2  # once per sample, not per frame
    assert result['libg_leaf_top'] == [{'file_offset': '0xf34c60', 'samples': 1}]
    assert result['whole_goal_complete'] is False


def test_mmap_timestamp_and_nonzero_file_offset_are_used(inputs):
    inputs['mappings'] += ('main 20 1.500000000: PERF_RECORD_MMAP2 20/20: '
                          f'[0x100000(0x2000000) @ 0x1000 00:1e 338 3313453382]: rwxp {ENGINE_PATH}\n')
    assert summarize(**inputs)['libg_leaf_top'][0]['file_offset'] == '0xf35c60'


def test_other_threads_are_counted_but_not_mislabeled_as_mainloop(inputs):
    inputs['script'] += sample('2.000000900', comm='Jit thread pool', pid=21,
                               frames=[(0x300, '/lib/libart.so')])
    result = summarize(**inputs)
    assert result['measured_thread_samples']['Jit thread pool'] == 1
    assert result['mainloop_libg_leaf_samples'] == 1


@pytest.mark.parametrize('exits', [[124, 0, 0], [0, 1, 0], [0, 0, 1], [0, 0]])
def test_reject_failed_or_missing_exit(inputs, exits):
    inputs['exits'] = exits
    with pytest.raises(ValueError, match='exit normally'):
        summarize(**inputs)


@pytest.mark.parametrize('old,new', [('use_clockid: 1', 'use_clockid: 0'),
                                    ('clockid: 1\n', 'clockid: 4\n'),
                                    ('cpu-clock:u', 'task-clock'),
                                    ('clockid: 1\n', 'clockid: 1\ncycles:u:')])
def test_reject_unverified_or_mixed_clocks_and_events(inputs, old, new):
    inputs['evlist'] = inputs['evlist'].replace(old, new)
    with pytest.raises(ValueError, match='CLOCK_MONOTONIC'):
        summarize(**inputs)


@pytest.mark.parametrize('report', ['# Total Lost Samples: 1\n', '',
                                    '# Total Lost Samples: 0\n# Total Lost Samples: 0\n'])
def test_reject_lost_or_missing_sample_accounting(inputs, report):
    inputs['report'] = report
    with pytest.raises(ValueError, match='lost'):
        summarize(**inputs)


def test_reject_wrong_engine_and_nonminimal_plan(inputs):
    bad = deepcopy(inputs)
    bad['plan']['identities']['/closure' + ENGINE_PATH] = '0' * 64
    with pytest.raises(ValueError, match='engine identity'):
        summarize(**bad)
    for key in ('NASHURA_PHASE_TIMING', 'NASHURA_TRACE_SNAPSHOTS', 'NASHURA_BATTLE_OBSERVATIONS'):
        bad = deepcopy(inputs)
        bad['plan']['environment'][key] = '1'
        with pytest.raises(ValueError, match='minimal benchmark'):
            summarize(**bad)


def test_reject_clock_shift_with_no_warm_window_overlap(inputs):
    inputs['script'] = inputs['script'].replace('2.', '900.')
    with pytest.raises(ValueError, match='insufficient'):
        summarize(**inputs)


@pytest.mark.parametrize('old,new', [('20/20:', '30/30:'), ('0.100000000:', '9.100000000:')])
def test_reject_mapping_from_wrong_process_or_future_time(inputs, old, new):
    inputs['mappings'] = inputs['mappings'].replace(old, new)
    with pytest.raises(ValueError, match='preceding process mapping'):
        summarize(**inputs)


@pytest.mark.parametrize('script', ['', 'Mainloop 20/20 2.000000000: cpu-clock:u:\n\n',
                                    'Mainloop 20/20 2.000000: cpu-clock:u:\n',
                                    sample('2.000000000') + 'unparsed data\n'])
def test_reject_empty_truncated_or_unrecognized_script(script):
    with pytest.raises(ValueError):
        sample_records(script)


def test_terminal_digest_and_java_completion_are_still_required(inputs):
    for old, new in [('digest=6c959326dd2f09cc', 'digest=0000000000000000'),
                     ('HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1', ''),
                     ('HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1', '')]:
        bad = dict(inputs, log=inputs['log'].replace(old, new))
        with pytest.raises(ValueError):
            summarize(**bad)
