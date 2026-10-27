"""Verify post-cold PN bridge evidence with the original Java persistence backend."""

import argparse
import hashlib
import json
from pathlib import Path

from e2_1_backup_trace import parse_backup
from e2_1_commit_events import commit_counts, delta
from e2_1_pn_matrix import CASES
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources
from verify_e2_1g_pn_codec import xml_entries


RUNS = ['baseline-final', 'original-final', 'replacement-retained-1', 'replacement-final-2']
SOURCES = ['E21PreferenceObserver.java', 'e2_1_return_observer.js', 'e2_1_config_probe.js',
           'e2_1_pn_bridge.c', 'e2_1_pn_bridge.h', 'e2_1_pn_bridge.js', 'e2_1_pn_bridge_matrix.py',
           'e2_1_pn_matrix.py', 'e2_1_backup_trace.py', 'e2_1_commit_events.py', 'run_e2_observed.py',
           'verify_e2_1h_pn_bridge.py', 'verify_e2_1g_pn_codec.py', 'verify_e0_snapshots.py',
           'verify_e1_gate.py', 'verify_e2_gate.py', 'e0_dependency_observer.js',
           'e2_jni_config_observer.js', 'e1_resource_redirect.js', 'e1_fmod_binding.js', 'e2_1_java_oracle.js']
RECEIPTS = [(2, 0, 0, 0), (2, 1, 2, 1), (1, 0, 0, 0), (1, 0, 0, 0),
            (1, 0, 0, 0), (2, 1, 2, 1), (1, 0, 0, 0), (2, 2, 3, 1),
            (2, 1, 2, 1), (2, 0, 0, 0), (2, 2, 3, 1)]
MATRIX_SCOPE = 'explicit sequential post-trajectory native PN entry; original Java per-store backend'
BRIDGE_SCOPE = 'post-cold-ready PN bridge; original per-store Cipher/XML/backup and JavaVM retained'


def bridge_snapshot(snapshot, installed, retired):
    require(snapshot['schema'] == 'e2.1h-pn-bridge.v1'
            and snapshot['installed'] is installed and snapshot['retired'] is retired
            and snapshot['errors'] == [] and snapshot['retained_allocations'] == 10
            and type(snapshot['fallbacks']) is int and snapshot['fallbacks'] >= 0,
            'bridge lifecycle, errors or retention differs')
    require(snapshot['scope'] == BRIDGE_SCOPE and snapshot['strict_gate'] == 'BLOCKED'
            and snapshot['original_xml_writer_replaced'] is False and snapshot['no_vm_verified'] is False
            and snapshot['backup_delivery'] == snapshot['concurrency_equivalence'] == 'unknown',
            'unsupported bridge promotion')


def signature(events):
    return [(row['store'], row['operation'], row['result']) for row in events]


def validate_matrix(matrix, original, final=None):
    replaced = final is not None
    require(matrix['schema'] == original['schema'] == 'e2.1h-actual-bridge-matrix.v1'
            and matrix['restored'] is True and matrix['restore_receipt'] == 'pn-restored\t2'
            and matrix['native_bridge_replacement'] is replaced
            and original['native_bridge_replacement'] is False, 'matrix mode or restoration differs')
    require(matrix['scope'] == MATRIX_SCOPE and matrix['original_xml_writer_replaced'] is False
            and matrix['no_vm_verified'] is False
            and matrix['backup_delivery'] == matrix['concurrency_equivalence'] == 'unknown',
            'unsupported matrix promotion')
    rows, reference = matrix['cases'], original['cases']
    require(len(rows) == len(reference) == len(CASES), 'matrix case set differs')
    before_xml = [xml_entries(record) for record in matrix['xml_before']]
    require(len(before_xml) == 2 and before_xml == [xml_entries(record) for record in matrix['xml_restored']],
            'original XML not fully restored')
    all_xml = [[xml_entries(record) for record in row['xml_after']] for row in rows]
    keys = []
    for store in range(2):
        changes = set()
        for pair in all_xml:
            require(len(pair) == 2, 'two actual XML stores required')
            changes.update(key for key in pair[store].keys() | before_xml[store].keys()
                           if pair[store].get(key) != before_xml[store].get(key))
        require(len(changes) == 1, 'unrelated preference changed or PN XML key not exercised')
        keys.append(next(iter(changes)))
    summaries = []
    for index, (row, expected, observed) in enumerate(zip(rows, CASES, reference)):
        require(all(row.get(key) == value for key, value in expected.items()), 'case order or input differs')
        require([entry['store'] for entry in row['before']] == ['o', 'p']
                and [entry['value'] for entry in row['before']] == expected['initial'], 'initial store state differs')
        require(row['before'] == observed['before'] and row['after'] == observed['after'],
                'original/replacement logical state or ciphertext differs')
        native, native_original = row['native'], observed['native']
        require(native['case'] == expected['case'] and native['operation'] == expected['operation']
                and native['key'] == 'PN_POPUP' and type(native['thread']) is int and native['thread'] > 0
                and native['exception_state']['before'] is False
                and native['exception_state']['after'] is False, 'native call provenance or JNI exception differs')
        ignored = {'thread'}
        require({key: value for key, value in native.items() if key not in ignored}
                == {key: value for key, value in native_original.items() if key not in ignored},
                'original/replacement native result differs')
        values = list(expected['initial'])
        if expected['operation'] == 'read':
            value = values[1] or values[0]
            require(native['entry'] == 'read_bridge' and native['entry_offset'] == 21022348
                    and native['return_type'] == 'EngineString16 through AArch64 x8'
                    and native['result'] == dict(length=len(value.encode()), value=value, bytes=list(value.encode())),
                    'actual native read differs')
            if not values[1] and values[0]:
                values[1] = values[0]
        else:
            require(native['entry'] == 'write_bridge' and native['entry_offset'] == 21018548
                    and native['input'] == expected['value']
                    and native['return_type'] == 'void; raw register not interpreted as status',
                    'actual native write contract differs')
            values = [expected['value']] * 2
        require([entry['store'] for entry in row['after']] == ['o', 'p']
                and [entry['value'] for entry in row['after']] == values, 'post-operation logical state differs')
        events = delta(row['events_before'], row['events_after'])
        original_events = delta(observed['events_before'], observed['events_after'])
        reads, writes, changed, backups = RECEIPTS[index]
        commits = {store + ':true': 1 for bit, store in enumerate(['o', 'p']) if changed & (1 << bit)}
        require(len({event['thread'] for event in events}) == 1
                and commit_counts(events) == commits and signature(events) == signature(original_events),
                'delegated order, window or actual commit differs')
        for store, entries in enumerate(all_xml[index]):
            key = keys[store]
            require({name: value for name, value in entries.items() if name != key}
                    == {name: value for name, value in before_xml[store].items() if name != key},
                    'unrelated XML preference changed')
            ciphertext = row['after'][store].get('encoded')
            require(bool(ciphertext) == bool(values[store]), 'empty value/ciphertext mismatch')
            expected_entry = None if ciphertext is None else ('string', (('name', key),), ciphertext, ())
            require(entries.get(key) == expected_entry, 'actual XML differs from original store')
        if replaced:
            first, last = row['bridge_before'], row['bridge_after']
            bridge_snapshot(first, True, False)
            bridge_snapshot(last, True, False)
            require(first['fallbacks'] == last['fallbacks'] == rows[0]['bridge_before']['fallbacks'],
                    'non-PN fallback changed during explicit PN matrix')
            require(len(first['events']) == index + 2 and last['events'][:-1] == first['events'],
                    'bridge matrix event window differs')
            event = last['events'][-1]
            require(event['operation'] == expected['operation'] and event['phase'] == 'explicit_post_trajectory_pn_matrix'
                    and event['caller'] == 'external-probe' and event['thread'] == native['thread']
                    and event['route'] == 'native-state-machine-java-backend' and event['status'] == 0
                    and event['receipt'] == dict(reads=reads, writes=writes, changed=changed,
                                                backup_method_returns=backups, failure=0),
                    'actual native C receipt or attribution differs')
        summaries.append(dict(case=expected['case'], commits=commits))
    if replaced:
        bridge_snapshot(final, False, True)
        require(final['events'] == rows[-1]['bridge_after']['events'] and len(final['events']) == 13,
                'bridge final event set differs')
        require(final['fallbacks'] == rows[-1]['bridge_after']['fallbacks'],
                'unattributed non-PN fallback after matrix')
        for event, phase in zip(final['events'][:2], ['after_reset', 'final_reset_state']):
            require(event['phase'] == phase and event['operation'] == 'read' and event['caller'] == 'external-probe'
                    and event['status'] == 0 and event['route'] == 'native-state-machine-java-backend'
                    and event['receipt'] == dict(reads=1, writes=0, changed=0, backup_method_returns=0, failure=0),
                    'explicit lifecycle probe attribution differs')
    return dict(cases=summaries, actual_commit_true=7, xml_restored=True, original_xml_writer_replaced=False,
                native_entry_scope='11 explicit sequential matrix calls; two additional explicit lifecycle reads',
                natural_battle_pn_calls_observed=0 if replaced else None,
                non_pn_original_bridge_fallbacks=final['fallbacks'] if replaced else None,
                fallback_key_names='not collected by PN bridge counter; unknown')


def validate_backup(raw):
    trace = parse_backup(raw)
    requests = [row for row in trace['calls'] if row['method'].endswith('.requestBackup()V')]
    changes = [row for row in trace['calls'] if row['method'].endswith('.dataChanged()V')]
    require(len(requests) == len(changes) == 5
            and all(row['completion'] == 'returned' for row in trace['calls'])
            and len({row['thread'] for row in trace['calls']}) == 1, 'backup method count/return/thread differs')
    for request, change in zip(requests, changes):
        require(request['begin_sequence'] < change['begin_sequence'] < change['end_sequence'] < request['end_sequence'],
                'backup delegation nesting differs')
    require(all(first['end_sequence'] < second['begin_sequence'] for first, second in zip(requests, requests[1:])),
            'backup request windows overlap')
    return dict(request_backup_returns=5, data_changed_returns=5,
                unnamed_method_count=trace['unnamed_method_count'], unnamed_records=trace['unnamed_records'],
                coverage=trace['coverage'], backup_delivery='unknown')


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1h-private-manifest.v1', 'wrong manifest schema')
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
    require(manifest['runs'] == RUNS, 'independent run set differs')
    artifacts = manifest['artifacts']
    require(set(artifacts) == {'library', 'dex'}, 'artifact set differs')
    hashes = {name: hashlib.sha256(raw(record['path'])).hexdigest() for name, record in artifacts.items()}
    recorded = load('build-identities.json')
    require(set(recorded) == set(artifacts) and all(recorded[name] == dict(record, sha256=hashes[name])
            for name, record in artifacts.items()), 'build artifact identity differs')
    build = load('build-reproduction.json')
    require(build['exit_code'] == 0 and build['rebuilt_sha256'] == hashes['library']
            and all(flag in build['argv'] for flag in ['-shared', '-fPIC', '-Werror'])
            and build['argv'][0].endswith('/aarch64-linux-android30-clang'), 'guest build not reproduced')
    for name in ['e2_1_pn_bridge.c', 'e2_1_pn_bridge.h']:
        require(build['sources'][name] == manifest['files'][manifest['verification_sources'][name]], 'build source differs')
    require(build['dex_exit_code'] == 0 and build['rebuilt_dex_sha256'] == hashes['dex']
            and build['sources']['E21PreferenceObserver.java']
            == manifest['files'][manifest['verification_sources']['E21PreferenceObserver.java']],
            'observer dex build not reproduced')
    commands = load('build-commands.json')
    require(len(commands) == 11 and all(row['exit_code'] == 0 for row in commands), 'bridge/observer build failed')
    for name, record in artifacts.items():
        require(any(row['argv'][-2:] == ['sha256sum', record['guest']]
                    and row['stdout'].split() == [hashes[name], record['guest']] for row in commands),
                'initial actual guest artifact identity missing')
    after = load('guest-artifacts-after.json')
    require(after['exit_code'] == 0 and after['identities'] == {record['guest']: hashes[name]
            for name, record in artifacts.items()}, 'final actual guest artifact identity differs')
    original = load(RUNS[0] + '/identity-before.json')
    require(len(original['files']) == 9, 'baseline original input identity set differs')
    matrices, results, processes = [], [], set()
    reference = load(RUNS[1] + '/pn-bridge-matrix.json')
    for index, run in enumerate(RUNS):
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        require(config['mode'] == 'original' and 'replacement' not in config and 'pn_codec' not in config,
                'unrelated configuration replacement enabled')
        require(load(run + '.config.json') == config and load(prefix + 'run-start.json')['config_sha256']
                == manifest['files'][run + '.config.json'], 'run config identity differs')
        completed_run(base, run, load(prefix + 'run-finished.json'),
                      [json.loads(line) for line in raw(prefix + 'messages.jsonl').splitlines() if line.strip()])
        process = load(prefix + 'owned-process.json')
        require(process['pid'] not in processes and process['stat'].startswith(str(process['pid']) + ' '),
                'independent process identity missing')
        processes.add(process['pid'])
        identity = load(prefix + 'identity-before.json')
        require(identity == load(prefix + 'identity-after.json') and identity['files'] == original['files']
                == config['guest_identity'] and identity['update_manifest_sha256']
                == config['update_identity_sha256'] == original['update_manifest_sha256'],
                'original inputs or update identity differ')
        commands = [json.loads(line) for line in raw(prefix + 'commands.jsonl').splitlines() if line.strip()]
        updates = [row for row in commands if row['argv'][-1].startswith('find ')
                   and 'update -type f -exec sha256sum' in row['argv'][-1]]
        require(len(updates) == 2 and all(row['code'] == 0 and len(row['stdout'].splitlines()) == 990
                and hashlib.sha256(row['stdout'].strip().encode()).hexdigest() == identity['update_manifest_sha256']
                for row in updates), '990-file before/after identity missing')
        require(raw(prefix + 'sources/run_e2_observed.py') == raw(manifest['verification_sources']['run_e2_observed.py'])
                and load(prefix + 'run-start.json')['runner_sha256'] == manifest['files'][prefix + 'sources/run_e2_observed.py'],
                'runner identity differs')
        for name, digest in load(prefix + 'source-identity.json').items():
            require(manifest['files'].get(prefix + 'sources/' + name) == digest, 'archived source identity differs')
            if name in SOURCES:
                require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                        'exercised source differs: ' + name)
        for name in (['E21PreferenceObserver.java', 'e2_1_pn_bridge_matrix.py'] if index else []) + (
                ['e2_1_pn_bridge.c', 'e2_1_pn_bridge.h'] if index >= 2 else []):
            require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                    'exercised bridge source differs: ' + name)
        for tick in range(3602):
            raw(prefix + f'snapshots/tick-{tick:06d}.bin')
        comparison = verify(base / RUNS[0] / 'snapshots', base / run / 'snapshots',
                            load(RUNS[0] + '/trace.json'), load(prefix + 'trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'native trajectory differs')
        require(controls(load(prefix + 'control-receipts.json')) == controls(load(RUNS[0] + '/control-receipts.json')),
                'native action/snapshot/restore/reset/terminal differs')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        observer = load(prefix + 'e2-final.json')
        require(observer['errors'] == [] and observer['dropped']['errors'] == observer['dropped']['config_calls'] == 0,
                'configuration observer errors or dropped events')
        if index:
            require(config['return_observer']['install_at'] == 'post-cold-ready'
                    and config['return_observer']['enable_pn_backend'] is True and config['pn_bridge_matrix'] is True
                    and config['return_observer']['dex_path'] == artifacts['dex']['guest'], 'observer contract differs')
            require(load(prefix + 'return-observer-uninstall.json')['result'] == 'uninstalled\t2'
                    and load(prefix + 'return-observer-release.json')['result'] == 'released\t6'
                    and load(prefix + 'return-observer-final.json')['references']
                    == dict(allocated_globals=1, deleted_globals=1, retired=True), 'observer not retired')
            matrix = load(prefix + 'pn-bridge-matrix.json')
            oracle = load(prefix + 'java-oracle-final.json')
            require(oracle['active'] is False and [row['operation'] for row in oracle['events']]
                    == ['startMethodTracing', 'stopMethodTracing']
                    and all(row['status'] == 'returned_without_pending_exception' for row in oracle['events']),
                    'ART tracing lifecycle differs')
            xml_sets = [('before', matrix['xml_before']), ('restored', matrix['xml_restored'])]
            xml_sets += [(row['case'], row['xml_after']) for row in matrix['cases']]
            for label, records in xml_sets:
                for store, record in enumerate(records):
                    require(raw(prefix + f'bridge-xml-{label}/store-{store}.xml') == bytes.fromhex(record['bytes_hex']),
                            'pulled actual XML differs')
            final = None
            if index >= 2:
                require(config['pn_bridge']['library'] == artifacts['library']['guest']
                        and config['pn_bridge']['mode'] == 'replacement', 'executed library/mode differs')
                installed = load(prefix + 'pn-bridge-install.json')
                bridge_snapshot(installed, True, False)
                require(installed['events'] == [] and installed['fallbacks'] == 0,
                        'bridge not installed before native lifecycle')
                final = load(prefix + 'pn-bridge-final.json')
            else:
                require('pn_bridge' not in config, 'original bridge replaced')
            matrices.append(dict(run=run, **validate_matrix(matrix, reference, final),
                                 backup=validate_backup(raw(prefix + 'java-methods.trace'))))
        else:
            require('pn_bridge' not in config and 'return_observer' not in config, 'baseline has PN mutation')
        results.append(dict(run=run, **{key: comparison[key] for key in EXPECTED_TRAJECTORY},
                            platform_asset_events=0, provider_failures=0, live_assets=0))
    return dict(schema='e2.1h-bounded-summary.v1', status='bounded_evidence_verified', strict_gate='BLOCKED',
                attachment_count=len(manifest['files']), runs=results, matrices=matrices,
                pn_native_replacement='post-cold-ready control logic only; original Java per-store backend retained',
                original_xml_writer_replaced=False, no_vm_verified=False, android_free_verified=False,
                training_admissible=False, tenfold_speedup_verified=False, removed_keys=0,
                unresolved=['cold-start PN bridge and key derivation', 'native persistence backend', 'backup delivery',
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
