"""Recompute serial four-input action distributions and serializer parity."""
import argparse
import gzip
import json
from pathlib import Path
import tempfile

from compare_headless_snapshots import compare
from run_headless_match import verify_completion
from summarize_headless_actions import summarize
from summarize_headless_native_phases import summarize as summarize_native
from summarize_headless_fourway import CASES, OBSERVATIONS, require_equal, sha256
from verify_combined_followup import validate_actions

GUEST_ROOT = Path('/home/gujiga/nashura-action-distributions-20261031-v1')
OBSERVER = '/dev/shm/nashura-headless-action-distributions-20261031-v1.so'
OBSERVER_SHA256 = '197d362c498d42c2dc7d899d0d879e773d61791c8c7057aab00c6fe9d540467c'
TOOL_TAR_SHA256 = '7876b92e7be378f193f2fb43b0e17c6b926b1ed85b3816452a581ac9448664a8'


def validate_environment(env, initial=None):
    memory = [x for x in env['meminfo'].splitlines() if x.startswith('MemTotal:')]
    if (env['cpu_count'] != 2 or env['affinity'] != [0, 1]
            or env['kernel'] != '6.8.0-138-generic' or env['epoch_ns'] <= 0
            or len(memory) != 1 or not 5800000 <= int(memory[0].split()[1]) <= 6291456):
        raise ValueError('fixed ARM64 VM environment required')
    if initial is not None and memory != [x for x in initial['meminfo'].splitlines() if x.startswith('MemTotal:')]:
        raise ValueError('guest MemTotal changed')


def read(path):
    return json.loads(Path(path).read_text())


def verify(root, reference):
    root, reference = Path(root), Path(reference)
    require_equal(read(root / 'stage-plan.json'), dict(cases=list(CASES), passes=2,
        paths=['cmd', 'inject'], baseline_runs=4, diagnostic_runs=16,
        observer_sha256=OBSERVER_SHA256, tool_tar_sha256=TOOL_TAR_SHA256,
        scope='bounded-action-distributions-diagnostic-only', benchmark=False, concurrency=False))
    require_equal(read(root / 'done.json'), dict(runs=20, successes=20, diagnostic_runs=16))
    order = [(c, 'cmd', 'baseline', False) for c in CASES]
    order += [(c, p, 'pass-' + str(i) + '-' + p, True)
              for i, cases in ((1, CASES), (2, tuple(reversed(CASES))))
              for c in cases for p in ('cmd', 'inject')]
    rows = read(root / 'results.json')
    if [(x['case'], x['path'], x['label'], x['diagnostic']) for x in rows] != order:
        raise ValueError('complete ordered baseline and forward/reverse diagnostic graph required')
    initial = read(root / 'initial-environment.json')
    validate_environment(initial)
    previous_end = initial['epoch_ns']
    results = []
    for name, path, label, diagnostic in order:
        case, prior = root / name, reference / name
        settings = read(case / ('settings-' + path + '.json'))
        original_settings = read(prior / 'settings.json')
        expected_settings = json.loads(json.dumps(original_settings))
        expected_settings['observer'] = OBSERVER
        expected_settings['identities'][OBSERVER] = OBSERVER_SHA256
        fixture = Path(__file__).with_name('fixtures') / ('headless-runtime-' + name + '.json')
        replay = read(fixture)
        if original_settings['identities'][original_settings['replay']] != sha256(fixture):
            raise ValueError('unchanged fixed replay required')
        if path == 'inject':
            replay['inject'], replay['cmd'] = replay['cmd'], []
            require_equal(read(case / 'inject-replay.json'), replay)
            expected_settings['replay'] = str(GUEST_ROOT / name / 'inject-replay.json')
            expected_settings['identities'][expected_settings['replay']] = sha256(case / 'inject-replay.json')
        require_equal(settings, expected_settings)
        directory = case / label
        plan, prior_plan = read(directory / 'plan.json'), read(prior / 'full-original/plan.json')
        expected_plan = json.loads(json.dumps(prior_plan))
        output = str(GUEST_ROOT / name / label)
        replacements = [(prior_plan['output'], output), (original_settings['observer'], OBSERVER),
                        (original_settings['replay'], settings['replay']),
                        (original_settings['identities'][original_settings['replay']], settings['identities'][settings['replay']])]
        def replace(value):
            for a, b in replacements:
                value = value.replace(a, b)
            return value
        expected_plan['command'] = [replace(x) for x in prior_plan['command']]
        expected_plan['environment'] = {k: replace(v) for k, v in prior_plan['environment'].items()}
        expected_plan.update(output=output, identities=settings['identities'], native_phases=diagnostic,
                             stdout_encoding='gzip-after-process-exit')
        if diagnostic:
            expected_plan['action_distributions'] = True
            expected_plan['environment'].update(NASHURA_NATIVE_PHASES='1', NASHURA_PHASE_TIMING='1',
                                                NASHURA_ACTION_DISTRIBUTIONS='1')
        require_equal(plan, expected_plan)
        if (directory / 'stdout.log').exists() or (directory / 'snapshots.bin').exists():
            raise ValueError('verified compressed evidence required')
        text = gzip.decompress((directory / 'stdout.log.gz').read_bytes()).decode()
        baseline = gzip.decompress((prior / 'full-original/stdout.log.gz').read_bytes()).decode()
        tick = verify_completion(int((directory / 'exit.txt').read_text()), text)
        if tick != verify_completion(int((prior / 'full-original/exit.txt').read_text()), baseline):
            raise ValueError('native final tick differs')
        validate_actions(text, replay)
        observations = [x for x in baseline.splitlines() if x.startswith(OBSERVATIONS)]
        if (not observations or [x for x in text.splitlines() if x.startswith(OBSERVATIONS)] != observations
                or any(x.startswith(('HEADLESS_NATIVE_OPTIMIZATION ', 'HEADLESS_BENCHMARK_METRICS ')) for x in text.splitlines())):
            raise ValueError('observed state differs or timing/optimization mixed in')
        with tempfile.TemporaryDirectory(prefix='nashura-actions-') as tmp:
            a, b = Path(tmp) / 'original.bin', Path(tmp) / 'diagnostic.bin'
            a.write_bytes(gzip.decompress((prior / 'full-original/snapshots.bin.gz').read_bytes()))
            b.write_bytes(gzip.decompress((directory / 'snapshots.bin.gz').read_bytes()))
            pair = compare(a, b, tick)
        if not pair['equal']:
            raise ValueError('serializer payload differs')
        result = read(directory / 'result.json')
        expected_result = dict(status='NATIVE_FINAL_OBSERVED_LIMITED', final_tick=tick,
            snapshot_trace=True, matches=1, record_hand_actions=False, native_optimization=None,
            native_phases=diagnostic, thread_resources=False, serial_batches=1, whole_goal_complete=False,
            records=pair['records'], archive_bytes=pair['right']['bytes'], archive_sha256=pair['right']['sha256'])
        if diagnostic:
            expected_result['action_distributions'] = True
        require_equal(result, expected_result)
        row = rows[len(results)]
        require_equal(row['result'], result)
        if (row['snapshot_gzip_sha256'] != sha256(directory / 'snapshots.bin.gz')
                or row['stdout_gzip_sha256'] != sha256(directory / 'stdout.log.gz')):
            raise ValueError('raw evidence identity differs')
        begin, end = (read(case / (label + suffix + '.json')) for suffix in ('-environment', '-end-environment'))
        for value in (begin, end):
            validate_environment(value, initial)
        if not previous_end < begin['epoch_ns'] < end['epoch_ns']:
            raise ValueError('strictly serial run windows required')
        previous_end = end['epoch_ns']
        action = summarize(text) if diagnostic else None
        if diagnostic:
            require_equal(read(directory / 'action-distributions.json'), action)
            require_equal(read(directory / 'native-phases.json'), summarize_native(text))
            mode3 = action['phases']['dispatch_mode3']['samples']
            if (len(mode3) != {'deck-a-seed-1': 50, 'deck-a-seed-7': 49,
                             'deck-b-seed-1': 22, 'deck-b-seed-7': 19}[name]
                    or action['phases']['dispatch_mode2']['calls']
                    or any(x['target_offset'] != 0xd30a00 for x in mode3)):
                raise ValueError('fixed four-input dispatch path differs')
        elif any(x.startswith(('HEADLESS_ACTION_', 'HEADLESS_NATIVE_PHASE')) for x in text.splitlines()):
            raise ValueError('baseline contains diagnostics')
        results.append(dict(case=name, path=path, label=label, diagnostic=diagnostic,
            frames=pair['records'], full_frames_equal=True, observed_equal=True,
            observed_lines=len(observations), archive_sha256=pair['right']['sha256'],
            action_distributions=action, start_epoch_ns=begin['epoch_ns'], end_epoch_ns=end['epoch_ns'],
            plan_sha256=sha256(directory / 'plan.json')))
    final = read(root / 'final-environment.json')
    validate_environment(final, initial)
    if final['epoch_ns'] <= previous_end:
        raise ValueError('final environment must follow all diagnostics')
    for name in CASES:
        sequences = [[(x['tick'], x['target_offset'], x['result']) for x in
                      row['action_distributions']['phases']['dispatch_mode3']['samples']]
                     for row in results if row['case'] == name and row['diagnostic']]
        if len(sequences) != 4 or any(x != sequences[0] for x in sequences[1:]):
            raise ValueError('cmd/inject and both passes must preserve dispatch sequence')
    return dict(schema='nashura.action-distributions-evidence.v1', host_recomputed=True,
        runs=len(results), diagnostic_runs=sum(x['diagnostic'] for x in results),
        full_frames=sum(x['frames'] for x in results), cases=results,
        benchmark=False, whole_goal_complete=False,
        limitations=['bounded diagnostic distributions include instrumentation overhead; no speedup claim',
            'injected parse and append occur before step; online input remains unmeasured',
            'serializer and observed fields do not cover all internal state',
            'four fixed inputs and two passes; no hardware cycles/cache or long-duration claim'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'reference', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.root, args.reference)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write(chr(10))
    print(json.dumps({k: v for k, v in result.items() if k != 'cases'}))


if __name__ == '__main__':
    main()
