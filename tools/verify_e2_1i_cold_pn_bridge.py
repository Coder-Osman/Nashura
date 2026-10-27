"""Verify natural cold-start PN control replacement, retaining the Java persistence backend."""

import argparse
import hashlib
import json
from pathlib import Path

from e2_1_commit_events import commit_counts, delta, parse_events
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources
from verify_e2_1d_evidence import validate_cold
from verify_e2_1h_pn_bridge import signature


RUNS = ['baseline-final', 'original-final', 'replacement-final-3', 'replacement-final-4']
SOURCES = ['E21PreferenceObserver.java', 'e2_1_return_observer.js', 'e2_1_pn_bridge.js',
           'e2_1_pn_bridge.c', 'e2_1_pn_bridge.h', 'run_e2_observed.py', 'e2_1_config_probe.js',
           'e2_1_commit_events.py', 'verify_e2_1i_cold_pn_bridge.py', 'verify_e2_1d_evidence.py',
           'verify_e2_1h_pn_bridge.py', 'verify_e0_snapshots.py', 'verify_e1_gate.py',
           'verify_e2_gate.py', 'e0_dependency_observer.js', 'e2_jni_config_observer.js',
           'e1_resource_redirect.js', 'e1_fmod_binding.js']
SCOPE = 'first-native-bridge PN control; original per-store Cipher/XML/backup and JavaVM retained'


def bridge_state(row, retired):
    require(row['schema'] == 'e2.1i-cold-pn-bridge.v1' and row['scope'] == SCOPE
            and row['installed'] is not retired and row['retired'] is retired
            and row['errors'] == [] and row['retained_allocations'] == 10
            and type(row['fallbacks']) is int and row['fallbacks'] >= 0,
            'cold PN bridge lifecycle or scope differs')
    require(row['strict_gate'] == 'BLOCKED' and row['no_vm_verified'] is False
            and row['original_xml_writer_replaced'] is False
            and row['backup_delivery'] == row['concurrency_equivalence'] == 'unknown',
            'unsupported cold PN promotion')


def pn_rows(observed, operation):
    require(observed['errors'] == [] and observed['dropped']['config_calls']
            == observed['dropped']['errors'] == 0, 'independent config observer incomplete')
    return [row for row in observed['config_calls'] if row['operation'] == operation
            and row.get('key', {}).get('value') == 'PN_POPUP']


def natural_values(observed):
    reads = [row for row in pn_rows(observed, 'PreferenceRead') if row['phase'] == 'configuration']
    writes = pn_rows(observed, 'PreferenceWrite')
    require(len(reads) == len(writes) == 1, 'natural PN upper entry set differs')
    read = reads[0]['returned_string']
    write = writes[0]['value']
    require(read['status'] == write['status'] == 'decoded', 'natural PN text unobserved')
    require(read['value'].isascii() and read['value'].isdecimal()
            and write['value'].isascii() and write['value'].isdecimal()
            and int(write['value']) == int(read['value']) + 1, 'natural PN increment differs')
    return read['value'], write['value']


def validate_replacement(initial, final, installed, bridge, observed, reference, uninstall, release):
    require(initial['references'] == dict(allocated_globals=1, deleted_globals=0, retired=False)
            and final['references'] == dict(allocated_globals=1, deleted_globals=1, retired=True)
            and uninstall == {'result': 'uninstalled\t2'} and release == {'result': 'released\t6'},
            'cold observer stores or references not retired')
    first, cold = initial['cold'], final['cold']
    for state in [first, cold]:
        require(state['armed'] is True and state['attempted'] is True and state['installed'] is True
                and state['installing'] is False and state['reentrant_entries'] == 0 and state['errors'] == []
                and state['install_result'] == 'installed\t2', 'cold PN backend failed')
        entry = state['install_entry']
        require(entry['entry'] == 1 and entry['operation'] == 'read' and entry['offset'] == 21022348
                and type(entry['thread']) is int and entry['thread'] > 0, 'cold installation entry differs')
    require(first['windows'] == cold['windows'] and len(cold['windows']) == 2
            and first['external_windows'] == 1 and cold['external_windows'] == 3
            and cold['entries'] >= first['entries'], 'natural/external cold windows differ')
    bridge_state(installed, False)
    bridge_state(bridge, True)
    events = bridge['events']
    require(len(installed['events']) == 3 and events[:3] == installed['events'] and len(events) == 5
            and bridge['fallbacks'] >= installed['fallbacks'], 'cold bridge event prefix differs')
    require(cold['entries'] == bridge['fallbacks'] + len(events), 'unattributed cold bridge entries')
    before_value, after_value = natural_values(observed)
    bases, windows = set(), []
    expected = [('read', '0x1394198', 'configuration', 'PreferenceRead', '0x76cae0', {}),
                ('write', '0x13942f0', 'cold_ready_wait', 'PreferenceWrite', '0x76cfcc',
                 {'o:true': 1, 'p:true': 1})]
    for index, (window, specification) in enumerate(zip(cold['windows'], expected)):
        operation, caller, phase, upper, upper_caller, commits = specification
        event = events[index]
        require(window['operation'] == event['operation'] == operation and window['caller'] == caller
                and event['caller'] == 'libg.so+' + caller and window['bridge_event'] == index
                and window['thread'] == event['thread'] and type(event['thread']) is int and event['thread'] > 0
                and window.get('completion') == 'original callsite return instruction',
                'natural native entry/callsite return differs')
        base, site = int(event['module_base'], 16), int(event['return_site'], 16)
        require(base > 0 and base % 4096 == 0 and site == base + int(caller, 16)
                and window['module_base'] == event['module_base']
                and window['return_site'] == event['return_site'], 'natural module base/callsite differs')
        bases.add(base)
        delegated = delta(window['before'], window['after'])
        original = delta(reference['cold']['windows'][index]['before'], reference['cold']['windows'][index]['after'])
        require(delegated and len({row['thread'] for row in delegated}) == 1
                and commit_counts(delegated) == commits and signature(delegated) == signature(original),
                'natural delegated commit or operation order differs')
        upper_rows = pn_rows(observed, upper)
        require(len(upper_rows) == 1 and upper_rows[0]['phase'] == phase
                and upper_rows[0]['caller'] == 'libg.so+' + upper_caller
                and upper_rows[0]['thread'] == event['thread'], 'independent natural phase/thread differs')
        receipt = dict(reads=1, writes=0, changed=0, backup_method_returns=0, failure=0) if not index else dict(
            reads=2, writes=2, changed=3, backup_method_returns=1, failure=0)
        require(event['receipt'] == receipt and event['status'] == 0
                and event['route'] == 'native-state-machine-java-backend', 'cold C transition receipt differs')
        require(event['result'] == before_value if not index else event['input'] == after_value,
                'cold bridge and independent PN text differ')
        windows.append(dict(operation=operation, caller=caller, independently_observed_phase=phase,
                            commits=commits, forwarded_order_equal=True))
    require(len(bases) == 1 and cold['windows'][0]['entry'] < cold['windows'][1]['entry'],
            'natural module or call order differs')
    for event, phase in zip(events[2:], ['cold_ready_wait', 'after_reset', 'final_reset_state']):
        require(event['operation'] == 'read' and event['caller'] == 'external-probe' and event['phase'] == phase
                and event['result'] == after_value and event['status'] == 0
                and event['route'] == 'native-state-machine-java-backend'
                and event['receipt'] == dict(reads=1, writes=0, changed=0, backup_method_returns=0, failure=0),
                'explicit readback/lifecycle attribution differs')
    return dict(natural_windows=windows, explicit_reads=3, non_pn_original_bridge_fallbacks=bridge['fallbacks'],
                natural_increment_verified=True, released_java_references=6,
                allocated_global_references=1, deleted_global_references=1,
                host_phase_is_not_internal_phase=True)


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1i-private-manifest.v1' and manifest['runs'] == RUNS,
            'wrong cold PN evidence schema/run set')
    base = base.resolve()
    for relative, expected in manifest['files'].items():
        path = Path(relative)
        target = base / path
        require(not path.is_absolute() and '..' not in path.parts
                and not any(parent.is_symlink() for parent in [target, *target.parents]), 'unsafe attachment path')
        require(hashlib.sha256(target.read_bytes()).hexdigest() == expected, 'attachment hash differs: ' + relative)

    def raw(relative):
        require(relative in manifest['files'], 'unbound attachment: ' + relative)
        return (base / relative).read_bytes()

    def load(relative):
        return json.loads(raw(relative))

    for name in SOURCES:
        require(raw(manifest['verification_sources'][name]) == Path(__file__).with_name(name).read_bytes(),
                'verification source differs: ' + name)
    artifacts = load('build-identities.json')
    require(set(artifacts) == {'library', 'dex'}, 'build artifact set differs')
    for record in artifacts.values():
        require(hashlib.sha256(raw(record['path'])).hexdigest() == record['sha256'], 'build artifact differs')
    build = load('build-commands.json')
    require(len(build) == 11 and all(row['exit_code'] == 0 for row in build), 'bridge/observer build failed')
    for record in artifacts.values():
        require(any(row['argv'][-2:] == ['sha256sum', record['guest']]
                    and row['stdout'].split() == [record['sha256'], record['guest']] for row in build),
                'guest build identity missing')
    reproduction = load('build-reproduction.json')
    require(reproduction['exit_code'] == reproduction['dex_exit_code'] == 0
            and reproduction['rebuilt_sha256'] == artifacts['library']['sha256']
            and reproduction['rebuilt_dex_sha256'] == artifacts['dex']['sha256']
            and reproduction['argv'][0].endswith('/aarch64-linux-android30-clang')
            and all(flag in reproduction['argv'] for flag in ['-shared', '-fPIC', '-Werror']),
            'bridge or observer build not reproduced')
    for name in ['e2_1_pn_bridge.c', 'e2_1_pn_bridge.h', 'E21PreferenceObserver.java']:
        require(reproduction['sources'][name] == manifest['files'][manifest['verification_sources'][name]],
                'reproduced build source differs')
    after = load('guest-artifacts-after.json')
    require(after['exit_code'] == 0 and after['identities'] == {
        record['guest']: record['sha256'] for record in artifacts.values()}, 'final guest artifact differs')
    reference = load('original-final/return-observer-final.json')
    original = load(RUNS[0] + '/identity-before.json')
    require(len(original['files']) == 9, 'original input identity count differs')
    results, processes = [], set()
    for index, run in enumerate(RUNS):
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        require(config['mode'] == 'original' and not any(key in config for key in
                ['replacement', 'pn_codec', 'pn_bridge_matrix', 'java_oracle']), 'unrelated replacement/probe enabled')
        require(config == load(run + '.config.json') and load(prefix + 'run-start.json')['config_sha256']
                == manifest['files'][run + '.config.json'], 'run configuration identity differs')
        completed_run(base, run, load(prefix + 'run-finished.json'),
                      [json.loads(row) for row in raw(prefix + 'messages.jsonl').splitlines() if row.strip()])
        owned = load(prefix + 'owned-process.json')
        require(owned['pid'] not in processes and owned['stat'].startswith(str(owned['pid']) + ' '),
                'independent native process identity differs')
        processes.add(owned['pid'])
        identity = load(prefix + 'identity-before.json')
        require(identity == load(prefix + 'identity-after.json') and identity['files']
                == original['files'] == config['guest_identity'] and identity['update_manifest_sha256']
                == original['update_manifest_sha256'] == config['update_identity_sha256'], 'frozen inputs differ')
        for record in artifacts.values():
            require(identity['files'][record['guest']] == record['sha256'], 'actual bridge/dex differs')
        commands = [json.loads(row) for row in raw(prefix + 'commands.jsonl').splitlines() if row.strip()]
        updates = [row for row in commands if row['argv'][-1].startswith('find ')
                   and 'update -type f -exec sha256sum' in row['argv'][-1]]
        require(len(updates) == 2 and all(row['code'] == 0 and len(row['stdout'].splitlines()) == 990
                and hashlib.sha256(row['stdout'].strip().encode()).hexdigest() == identity['update_manifest_sha256']
                for row in updates), '990-file before/after identity missing')
        require(raw(prefix + 'sources/run_e2_observed.py') == raw(manifest['verification_sources']['run_e2_observed.py'])
                and load(prefix + 'run-start.json')['runner_sha256'] == manifest['files'][prefix + 'sources/run_e2_observed.py'],
                'executed runner differs')
        for name, digest in load(prefix + 'source-identity.json').items():
            require(manifest['files'].get(prefix + 'sources/' + name) == digest, 'archived source identity differs')
            if name in SOURCES:
                require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                        'executed source differs: ' + name)
        for tick in range(3602):
            raw(prefix + f'snapshots/tick-{tick:06d}.bin')
        comparison = verify(base / RUNS[0] / 'snapshots', base / run / 'snapshots',
                            load(RUNS[0] + '/trace.json'), load(prefix + 'trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'native trajectory differs')
        require(controls(load(prefix + 'control-receipts.json')) == controls(load(RUNS[0] + '/control-receipts.json')),
                'native actions/snapshot/restore/reset/terminal differ')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        result = dict(run=run, **{key: comparison[key] for key in EXPECTED_TRAJECTORY},
                      platform_asset_events=0, provider_failures=0, live_assets=0)
        if index:
            settings = config['return_observer']
            require(settings['install_at'] == 'first-native-bridge' and settings['release_after_uninstall'] is True
                    and settings['dex_path'] == artifacts['dex']['guest'], 'cold observer configuration differs')
            for name in ['E21PreferenceObserver.java', 'e2_1_pn_bridge.c', 'e2_1_pn_bridge.h'] if index >= 2 else ['E21PreferenceObserver.java']:
                require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                        'executed backend source differs')
            parse_events(raw(prefix + 'return-observer-events.tsv').decode())
            observer = load(prefix + 'e2-final.json')
            initial, final = load(prefix + 'return-observer-cold.json'), load(prefix + 'return-observer-final.json')
            uninstall, release = load(prefix + 'return-observer-uninstall.json'), load(prefix + 'return-observer-release.json')
            if index == 1:
                require('pn_bridge' not in config, 'reference natural bridge replaced')
                result['cold'] = validate_cold(initial, final, observer, uninstall, release)
                natural_values(observer)
            else:
                require(config['pn_bridge']['install_at'] == 'first-native-bridge'
                        and config['pn_bridge']['mode'] == 'replacement'
                        and config['pn_bridge']['library'] == artifacts['library']['guest']
                        and settings['enable_pn_backend'] is True, 'cold PN replacement configuration differs')
                result['cold'] = validate_replacement(initial, final, load(prefix + 'pn-bridge-install.json'),
                    load(prefix + 'pn-bridge-final.json'), observer, reference, uninstall, release)
        else:
            require('pn_bridge' not in config and 'return_observer' not in config, 'baseline PN mutation')
        results.append(result)
    previous, following = RUNS[2:]
    terminal = load(previous + '/semantics-terminal-reset.json')[0]
    next_read = load(following + '/pn-bridge-final.json')['events'][0]['result']
    require(terminal['exception_state'] == dict(before=False, after=False,
                source='explicit JNIEnv.ExceptionCheck on probe thread')
            and terminal['result']['value'] == bytes(terminal['result']['bytes']).decode() == next_read,
            'independent replacement process readback differs')
    for run in RUNS:
        cleanup, owned = load(run + '/cleanup.json'), load(run + '/owned-process.json')
        require(cleanup['action'] == 'force-stop completed owned attempt' and cleanup['pid'] == owned['pid']
                and cleanup['stat'].split(') ', 1)[1].split()[19] == owned['stat'].split(') ', 1)[1].split()[19],
                'owned process cleanup identity differs')
    return dict(schema='e2.1i-bounded-summary.v1', status='bounded_evidence_verified', runs=results,
                attachment_count=len(manifest['files']), independent_process_readback_equal=True,
                pn_native_replacement='natural cold-start control only; original Java persistence retained',
                strict_gate='BLOCKED', removed_keys=0, original_xml_writer_replaced=False,
                no_vm_verified=False, android_free_verified=False, training_admissible=False,
                tenfold_speedup_verified=False, backup_delivery='unknown', concurrency_equivalence='unknown',
                unresolved=['native persistence and key derivation', 'backup delivery',
                            'concurrency equivalence and crash durability', '46-key removability'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(json.loads(args.manifest.read_text()), args.manifest.resolve().parent)
    result['private_manifest_sha256'] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    with args.output.open('x') as target:
        json.dump(result, target, indent=2)
        target.write('\n')
    print(json.dumps(dict(status=result['status'], strict_gate=result['strict_gate'], attachment_count=result['attachment_count'])))


if __name__ == '__main__':
    main()
