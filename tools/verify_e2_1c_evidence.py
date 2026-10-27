"""Verify bounded PN_POPUP commit evidence; never promote the E2.1 strict gate."""

import argparse
import hashlib
import json
from pathlib import Path

from e2_1_commit_events import competing_values, failure_path, parse_events, sequential, validate_case
from e2_1_partial_method_trace import parse_partial
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources


SOURCES = ['E21PreferenceObserver.java', 'e2_1_return_observer.js', 'e2_1_race_probe.c',
           'e2_1_race_probe.js', 'e2_1_concurrent_probe.c', 'e2_1_config_probe.js',
           'e2_1_java_oracle.js', 'e2_1_commit_events.py', 'e2_1_method_trace.py', 'e2_1_partial_method_trace.py',
           'verify_e2_1c_evidence.py', 'run_e2_observed.py']


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1c-private-manifest.v1', 'wrong evidence schema')
    for relative, expected in manifest['files'].items():
        path = Path(relative)
        require(not path.is_absolute() and '..' not in path.parts, 'unsafe attachment path')
        target = base / path
        require(not any(parent.is_symlink() for parent in [target, *target.parents]), 'symlink attachment')
        require(hashlib.sha256(target.read_bytes()).hexdigest() == expected, 'attachment hash differs: ' + relative)

    def raw(relative):
        require(relative in manifest['files'], 'unbound attachment: ' + relative)
        return (base / relative).read_bytes()

    def load(relative):
        return json.loads(raw(relative))

    for name in SOURCES:
        require(raw(manifest['verification_sources'][name]) == Path(__file__).with_name(name).read_bytes(),
                'active verification source differs: ' + name)
    baseline = manifest['baseline']
    runs = manifest['runs']
    require(len(set(runs)) == len(runs) == 6 and runs[0] == baseline, 'six independent native runs required')
    original_identity = load(baseline + '/identity-before.json')
    baseline_controls = controls(load(baseline + '/control-receipts.json'))
    baseline_trace = load(baseline + '/trace.json')
    processes = set()
    run_results = []
    for run in runs:
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        require(config['mode'] == 'original' and 'replacement' not in config, 'original bridge required')
        messages = [json.loads(line) for line in raw(prefix + 'messages.jsonl').splitlines() if line.strip()]
        completed_run(base, run, load(prefix + 'run-finished.json'), messages)
        pid = load(prefix + 'owned-process.json')['pid']
        require(pid not in processes, 'process reused')
        processes.add(pid)
        require(load(prefix + 'identity-before.json') == load(prefix + 'identity-after.json') == original_identity,
                'frozen input identity changed')
        require(config['guest_identity'] == original_identity['files']
                and config['update_identity_sha256'] == original_identity['update_manifest_sha256'],
                'configuration identity differs')
        start = load(prefix + 'run-start.json')
        require(start['runner_sha256'] == manifest['files'][prefix + 'sources/run_e2_observed.py'],
                'runner source differs')
        source_identity = load(prefix + 'source-identity.json')
        require(all(manifest['files'].get(prefix + 'sources/' + name) == digest
                    for name, digest in source_identity.items()), 'archived source identity differs')
        for tick in range(3602):
            raw(prefix + f'snapshots/tick-{tick:06d}.bin')
        comparison = verify(base / baseline / 'snapshots', base / run / 'snapshots',
                            baseline_trace, load(prefix + 'trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'trajectory differs')
        require(controls(load(prefix + 'control-receipts.json')) == baseline_controls, 'lifecycle receipt differs')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        observed = 'return_observer' in config
        if observed:
            require(load(prefix + 'return-observer-install.json') == dict(
                result='installed\t2', phase='after_cold_ready_before_state_create'), 'observer window differs')
            require(load(prefix + 'return-observer-uninstall.json')['result'] == 'uninstalled\t2', 'restore failed')
            parse_events(raw(prefix + 'return-observer-events.tsv').decode())
        run_results.append(dict(run=run, observed=observed, **comparison))
    require([row['observed'] for row in run_results] == [False, True, False, True, False, False],
            'observer/baseline sequence differs')
    current = manifest['current_observer_run']
    require(current in runs and raw(current + '/sources/E21PreferenceObserver.java')
            == Path(__file__).with_name('E21PreferenceObserver.java').read_bytes(), 'current observer not trajectory tested')
    sequence = manifest['sequential_probe']
    seq_rows = load(sequence + '/results.json')
    sequential_result = sequential(seq_rows)
    require(raw(sequence + '/install.txt').decode() == 'installed\t2'
            and raw(sequence + '/uninstall.txt').decode() == 'uninstalled\t2', 'sequential install/restore differs')
    parse_events(raw(sequence + '/final-events.tsv').decode())
    probe = manifest['failure_race_probe']
    require(load(probe + '/failures.json') == [], 'diagnostic attempt incomplete')
    require(raw(probe + '/input-identity-before.txt') == raw(probe + '/input-identity-after.txt')
            and raw(probe + '/update-identity-before.txt') == raw(probe + '/update-identity-after.txt'),
            'diagnostic input identity changed')
    diagnostic_identity = {line.split(maxsplit=1)[1]: line.split()[0]
                           for line in raw(probe + '/input-identity-before.txt').decode().splitlines()}
    require(diagnostic_identity == original_identity['files']
            and hashlib.sha256(raw(probe + '/update-identity-before.txt')).hexdigest()
            == original_identity['update_manifest_sha256'], 'diagnostic input differs from trajectory baseline')
    require(raw(probe + '/install.txt').decode() == 'installed\t2'
            and raw(probe + '/uninstall.txt').decode() == 'uninstalled\t2', 'diagnostic restore differs')
    parse_events(raw(probe + '/events.tsv').decode())
    require(raw(probe + '/sources/E21PreferenceObserver.java') == raw(current + '/sources/E21PreferenceObserver.java'),
            'diagnostic observer not trajectory tested')
    for name in ['E21PreferenceObserver.java', 'e2_1_return_observer.js', 'e2_1_race_probe.c',
                 'e2_1_race_probe.js', 'e2_1_concurrent_probe.c']:
        require(raw(probe + '/sources/' + name) == raw(manifest['verification_sources'][name]),
                'diagnostic source differs: ' + name)
    require(not any(row['payload'].get('type') == 'error' for row in load(probe + '/messages.json')),
            'diagnostic script error')
    commands = load(probe + '/commands.json')
    require(all(row['exit_code'] == 0 for row in commands), 'diagnostic command failed')
    for artifact, guest in manifest['helper_artifacts'].items():
        expected = hashlib.sha256(raw(artifact)).hexdigest()
        require(any(row['argv'] == ['shell', 'sha256sum', guest]
                    and row['stdout'].split() == [expected, guest] for row in commands),
                'helper installed bytes not verified')
    build = manifest['reproduced_build']
    require(all(row['exit_code'] == 0 for row in load(build + '/commands.json')), 'helper reproduction failed')
    for artifact, reproduced in [('dex-v2/classes.dex', build + '/dex/classes.dex'),
                                 ('libe21race.so', build + '/libe21race.so')]:
        require(artifact in manifest['helper_artifacts'] and raw(artifact) == raw(reproduced),
                'helper build bytes differ')
    validate_case(load(probe + '/normal-case.json'), {'o:true': 1, 'p:true': 1})
    failure_trace = parse_partial(raw(probe + '/failure-methods.trace'))
    failure_result = failure_path(load(probe + '/failure-cases.json'), failure_trace)
    require(raw(probe + '/disk-before-failure.txt') == raw(probe + '/disk-after-failure.txt'), 'disk bytes changed')
    modes = raw(probe + '/fault-modes.txt').decode().splitlines()
    require(len(modes) == 3 and sorted(line.split()[0] for line in modes) == ['440', '440', '555'],
            'fault permission evidence differs')
    require(load(manifest['post_probe_modes']) == load(probe + '/original-modes.json'),
            'diagnostic permissions not restored')
    log = raw(probe + '/failure-logcat.txt').decode()
    for filename in ['storage.xml', 'storage_new.xml']:
        expected = "SharedPreferencesImpl: Couldn't rename file /data/user/0/nullsroyale.rel.free/shared_prefs/"
        expected += filename + ' to backup file /data/user/0/nullsroyale.rel.free/shared_prefs/' + filename + '.bak'
        require(sum(expected in line for line in log.splitlines()) == 1, 'actual backend rename failure missing')
    failure_rows = load(probe + '/failure-cases.json')
    failed_value = failure_rows[0]['native']['input']
    require(raw(probe + '/stores-after-failure.txt').decode().splitlines() == [failed_value, failed_value],
            'two-store failed-commit memory differs')
    recovery = load(probe + '/recovery.json')
    require(recovery['native']['input'] != failed_value, 'recovery same-value skip')
    validate_case(recovery, {'o:true': 1, 'p:true': 1})
    race_trace = parse_partial(raw(probe + '/race-methods.trace'))
    race_result = competing_values(load(probe + '/race.json'), race_trace)
    restoration = load(probe + '/restoration.json')
    require([row['case'] for row in restoration] == ['restore', 'restored_read'], 'final restoration cases differ')
    require(restoration[0]['native']['input'] == restoration[1]['native']['result']['value']
            == load(probe + '/saved.json')['value'], 'saved PN was not restored')
    require(raw(probe + '/stores-restored.txt').decode().splitlines() == [load(probe + '/saved.json')['value']] * 2,
            'two-store restoration differs')
    validate_case(restoration[0], {'o:true': 1, 'p:true': 1})
    validate_case(restoration[1], {})
    first_failure = load(manifest['retained_failure'])
    require(len(first_failure) == 1 and first_failure[0]['stage'] == 'write-failure', 'failed attempt lost')
    return dict(schema='e2.1c-bounded-summary.v1', status='bounded_evidence_verified',
                attachment_count=len(manifest['files']), runs=run_results, sequential=sequential_result,
                failure=failure_result, race=race_result, retained_failed_attempts=1,
                art_coverage=dict(exhaustive=False, unnamed_semantics='unknown',
                                  failure_unnamed_methods=len(failure_trace['unnamed_methods']),
                                  race_unnamed_methods=len(race_trace['unnamed_methods']),
                                  failure_unnamed_records=sum(row['records'] for row in failure_trace['unnamed_methods']),
                                  race_unnamed_records=sum(row['records'] for row in race_trace['unnamed_methods']),
                                  original_strict_parser='unchanged; rejects unnamed method IDs'),
                strict_gate='BLOCKED', no_vm_verified=False, android_free_verified=False,
                training_admissible=False, tenfold_speedup_verified=False,
                pn_native_replacement=False, removed_keys=0,
                scope='PN_POPUP only; temporary forwarding wrappers after cold-ready; real Java stores remain',
                unresolved=['46-key removability', 'natural cold-start commit return values',
                            'native replacement persistence/failure equivalence',
                            'general concurrency and crash durability', 'Java reference retirement'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(json.loads(args.manifest.read_text()), args.manifest.resolve().parent)
    with args.output.open('x') as target:
        json.dump(result, target, indent=2)
        target.write('\n')
    print(json.dumps(dict(status=result['status'], attachment_count=result['attachment_count'],
                          strict_gate=result['strict_gate'], no_vm_verified=False)))


if __name__ == '__main__':
    main()
