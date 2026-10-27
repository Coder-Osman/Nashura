"""Validate bounded original-bridge cold-start evidence; never promote the strict gate."""

import argparse
import hashlib
import json
from pathlib import Path

from e2_1_commit_events import commit_counts, delta, parse_events
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources


SOURCES = ['E21PreferenceObserver.java', 'e2_1_return_observer.js', 'run_e2_observed.py',
           'e2_1_commit_events.py', 'verify_e2_1d_evidence.py', 'e2_1d_public_summary.py',
           'verify_e0_snapshots.py', 'verify_e1_gate.py', 'verify_e2_gate.py']


def validate_cold(initial, final, observed, uninstall, release):
    require(initial['references'] == dict(allocated_globals=1, deleted_globals=0, retired=False),
            'initial references differ')
    require(final['references'] == dict(allocated_globals=1, deleted_globals=1, retired=True),
            'observer references not retired')
    require(uninstall == {'result': 'uninstalled\t2'} and release == {'result': 'released\t6'},
            'original stores not restored or helper references not released')
    first, cold = initial['cold'], final['cold']
    for state in [first, cold]:
        require(state['armed'] is True and state['attempted'] is True and state['installed'] is True
                and state['installing'] is False and state['errors'] == [], 'cold observer failed')
        require(state['install_result'] == 'installed\t2' and state['install_entry']['entry'] == 1
                and state['install_entry']['operation'] == 'read'
                and state['install_entry']['offset'] == 21022348, 'first bridge installation differs')
    require(len(first['windows']) == len(cold['windows']) == 2
            and first['windows'] == cold['windows'], 'missing, empty or changed natural windows')
    require(cold['external_windows'] == 3 and cold['entries'] >= first['entries'],
            'external direct probes not separated')
    require(observed['errors'] == [] and observed['dropped']['config_calls'] == 0
            and observed['dropped']['errors'] == 0, 'independent config observer incomplete')
    results, bases = [], set()
    specifications = [('read', '0x1394198', 'configuration', {}),
                      ('write', '0x13942f0', 'cold_ready_wait', {'o:true': 1, 'p:true': 1})]
    for window, (operation, caller, phase, expected) in zip(cold['windows'], specifications):
        require(window['operation'] == operation and window['caller'] == caller
                and window.get('completion') == 'original callsite return instruction'
                and 'after' in window, 'original callsite return not observed')
        site = int(window['return_site'], 16)
        base = site - int(caller, 16)
        require(site > base > 0 and base % 4096 == 0, 'return site/base inconsistent')
        bases.add(base)
        events = delta(window['before'], window['after'])
        require(events and len({row['thread'] for row in events}) == 1, 'Java window is not sequential')
        require(commit_counts(events) == expected, 'actual delegated commit result differs')
        native_operation = 'PreferenceNative' + operation.title() + 'Bridge'
        rows = [row for row in observed['config_calls'] if row['operation'] == native_operation
                and row.get('key', {}).get('value') == 'PN_POPUP'
                and row.get('caller') == 'libg.so+' + caller]
        require(len(rows) == 1 and rows[0]['phase'] == phase and rows[0]['thread'] == window['thread'],
                'independent natural bridge provenance differs')
        results.append(dict(operation=operation, phase=phase, caller=caller, commits=expected))
    require(len(bases) == 1, 'return sites belong to different module bases')
    require(cold['windows'][0]['entry'] < cold['windows'][1]['entry'], 'natural call order differs')
    return dict(windows=results, native_entries=cold['entries'], bridge_onleave_events=cold['returns'],
                external_windows=cold['external_windows'], released_java_references=6,
                allocated_global_references=1, deleted_global_references=1)


def restart_readback(terminal, observed, cleanup, owned):
    require(len(terminal) == 1 and terminal[0]['case'] == 'after_terminal_reset'
            and terminal[0]['key'] == 'PN_POPUP', 'wrong terminal readback')
    row = terminal[0]
    require(row['exception_state']['before'] is False and row['exception_state']['after'] is False,
            'terminal JNI exception')
    reads = [item for item in observed['config_calls'] if item['operation'] == 'PreferenceJNIReadBridge'
             and item.get('key', {}).get('value') == 'PN_POPUP' and item['phase'] == 'configuration']
    require(len(reads) == 1 and reads[0]['returned_string']['status'] == 'decoded'
            and row['operation'] == 'read' and row['result']['length'] == len(row['result']['bytes'])
            and bytes(row['result']['bytes']).decode('utf-8') == row['result']['value']
            and reads[0]['returned_string']['value'] == row['result']['value'], 'restart readback differs')
    require(cleanup['action'] == 'force-stop completed owned attempt' and cleanup['pid'] == owned['pid']
            and cleanup['stat'].split(') ', 1)[1].split()[19]
            == owned['stat'].split(') ', 1)[1].split()[19], 'previous process cleanup identity differs')


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1d-private-manifest.v1', 'wrong evidence schema')
    base = base.resolve()
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
                'verification source differs: ' + name)
    baseline, runs = manifest['baseline'], manifest['success_runs']
    require(baseline == 'baseline' and runs == ['cold-3', 'cold-4'], 'bounded run set differs')
    original = load(baseline + '/identity-before.json')
    require(len(original['files']) == 7, 'frozen original input count differs')
    dex = manifest['helper_artifact']
    dex_hash = hashlib.sha256(raw(dex['path'])).hexdigest()
    commands = load('observer-build/commands.json')
    require(len(commands) == 4 and all(row['exit_code'] == 0 for row in commands), 'helper build/upload failed')
    require(commands[0]['argv'][0].endswith('/javac') and '--release' in commands[0]['argv']
            and commands[1]['argv'][0].endswith('/d8') and '--lib' in commands[1]['argv']
            and '--min-api' in commands[1]['argv'], 'helper compiler provenance missing')
    require(commands[2]['argv'][-1] == dex['guest'] and commands[2]['argv'][-3] == 'push'
            and commands[3]['argv'][-3:] == ['shell', 'sha256sum', dex['guest']]
            and raw('observer-build/guest-hash.stdout').decode().split() == [dex_hash, dex['guest']],
            'installed helper digest not verified')
    processes, results = set(), []
    for run in [baseline, *runs]:
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        require(config['mode'] == 'original' and 'replacement' not in config, 'original Java bridge required')
        messages = [json.loads(line) for line in raw(prefix + 'messages.jsonl').splitlines() if line.strip()]
        require(not any(message.get('type') == 'error' for message in messages), 'Frida runtime error')
        completed_run(base, run, load(prefix + 'run-finished.json'), messages)
        owned = load(prefix + 'owned-process.json')
        require(owned['pid'] not in processes, 'process reused')
        processes.add(owned['pid'])
        identity = load(prefix + 'identity-before.json')
        require(identity == load(prefix + 'identity-after.json'), 'input changed during native run')
        expected = dict(original['files'])
        if run != baseline:
            expected[dex['guest']] = dex_hash
        require(identity['files'] == config['guest_identity'] == expected
                and identity['update_manifest_sha256'] == config['update_identity_sha256']
                == original['update_manifest_sha256'], 'frozen inputs or update identity differ')
        require(load(prefix + 'run-start.json')['runner_sha256'] == manifest['files'][prefix + 'sources/run_e2_observed.py'],
                'runner identity differs')
        source_identity = load(prefix + 'source-identity.json')
        require(all(manifest['files'].get(prefix + 'sources/' + name) == digest
                    for name, digest in source_identity.items()), 'run source archive differs')
        for tick in range(3602):
            raw(prefix + f'snapshots/tick-{tick:06d}.bin')
        comparison = verify(base / baseline / 'snapshots', base / run / 'snapshots',
                            load(baseline + '/trace.json'), load(prefix + 'trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'trajectory differs')
        require(controls(load(prefix + 'control-receipts.json')) == controls(load(baseline + '/control-receipts.json')),
                'snapshot/restore/reset/terminal controls differ')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        row = dict(run=run, **comparison)
        if run != baseline:
            settings = config['return_observer']
            require(settings['install_at'] == 'first-native-bridge' and settings['release_after_uninstall'] is True
                    and settings['dex_path'] == dex['guest'], 'cold observer configuration differs')
            for name in ['E21PreferenceObserver.java', 'e2_1_return_observer.js', 'run_e2_observed.py']:
                require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                        'current observer not native trajectory tested')
            parse_events(raw(prefix + 'return-observer-events.tsv').decode())
            row['cold'] = validate_cold(load(prefix + 'return-observer-cold.json'),
                load(prefix + 'return-observer-final.json'), load(prefix + 'e2-final.json'),
                load(prefix + 'return-observer-uninstall.json'), load(prefix + 'return-observer-release.json'))
        results.append(row)
    restart_readback(load(runs[0] + '/semantics-terminal-reset.json'), load(runs[1] + '/e2-final.json'),
                     load(runs[0] + '/cleanup.json'), load(runs[0] + '/owned-process.json'))
    for run, exit_code in [('cold-1', 0), ('cold-2', 1)]:
        failed = load(run + '/return-observer-final.json')['cold']
        require(failed['windows'] and any('after' not in row for row in failed['windows']),
                'retained observation failure missing')
        require(load(run + '/run-finished.json')['exit_code'] == exit_code, 'retained exit differs')
    require('return-observer-cold-windows' in load('cold-2/evidence-incomplete.json')['missing_final_snapshots'],
            'fail-closed rejection not retained')
    return dict(schema='e2.1d-bounded-summary.v1', status='bounded_evidence_verified',
                attachment_count=len(manifest['files']), runs=results, restart_readback_equal=True,
                retained_incomplete_runs=2, strict_gate='BLOCKED', no_vm_verified=False,
                android_free_verified=False, training_admissible=False, tenfold_speedup_verified=False,
                pn_native_replacement=False, removed_keys=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(json.loads(args.manifest.read_text()), args.manifest.resolve().parent)
    with args.output.open('x') as target:
        json.dump(result, target, indent=2)
        target.write('\n')
    print(json.dumps(dict(status=result['status'], strict_gate=result['strict_gate'],
                          attachment_count=result['attachment_count'])))


if __name__ == '__main__':
    main()
