"""Verify original PN priority/backfill against an independent native candidate."""

import argparse
import hashlib
import json
from pathlib import Path

from e2_1_commit_events import commit_counts, delta
from e2_1_pn_matrix import CASES
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources


SOURCES = ['E21PreferenceObserver.java', 'e2_1_return_observer.js', 'e2_1_config_probe.js',
           'e2_1_pn_matrix.py', 'e2_1_pn_matrix_candidate.js', 'e2_1_pn_store.c', 'e2_1_pn_store.h',
           'e2_1_commit_events.py', 'run_e2_observed.py', 'verify_e2_1f_pn_matrix.py',
           'verify_e0_snapshots.py', 'verify_e1_gate.py', 'verify_e2_gate.py']
RUNS = ['baseline-current', 'matrix-1', 'matrix-2']


def validate_matrix(matrix, final):
    require(matrix['schema'] == 'e2.1-pn-original-matrix.v1' and matrix['restored'] is True
            and matrix['restore_receipt'] == 'pn-restored\t2', 'raw PN restoration missing')
    require(matrix['backup_delivery'] == 'unknown' and matrix['native_bridge_replacement'] is False
            and matrix['no_vm_verified'] is False, 'unsupported matrix promotion')
    require(final['schema'] == 'e2.1-pn-matrix-candidate.v1'
            and final['original_storage_format_compatibility'] is False
            and final['native_bridge_replacement'] is False and final['no_vm_verified'] is False,
            'unsupported candidate promotion')
    rows = matrix['cases']
    require([row['case'] for row in rows] == [row['case'] for row in CASES], 'matrix cases differ')
    require([row['candidate'] for row in rows] == final['results'], 'final candidate results differ')
    summaries = []
    for row, expected in zip(rows, CASES):
        require(all(row.get(key) == value for key, value in expected.items()), 'matrix input differs')
        for state in ['before', 'after']:
            require([item['store'] for item in row[state]] == ['o', 'p'], 'store order differs')
            require(all(set(item) <= {'store', 'value', 'plain', 'encoded'}
                        and item.get('plain') is None for item in row[state]), 'unexpected raw PN state')
        before = [item['value'] for item in row['before']]
        require(before == expected['initial'], 'original pair setup differs')
        after = list(before)
        if expected['operation'] == 'read':
            value = before[1] or before[0]
            if not before[1] and before[0]:
                after[1] = before[0]
        else:
            value = expected['value']
            after = [value, value]
        changed = sum(1 << index for index in range(2) if before[index] != after[index])
        original = row['original']
        require(original['case'] == expected['case'] and original['key'] == 'PN_POPUP'
                and original['operation'] == expected['operation']
                and type(original['thread']) is int and original['thread'] > 0
                and original['exception_state']['before'] is False
                and original['exception_state']['after'] is False, 'original provenance or JNI exception differs')
        require(original['entry'] == expected['operation'] + '_bridge'
                and original['entry_offset'] == (21022348 if expected['operation'] == 'read' else 21018548),
                'original bridge identity differs')
        candidate = row['candidate']
        require(candidate['case'] == expected['case'] and candidate['operation'] == expected['operation']
                and candidate['initial'] == before and candidate['status'] == candidate['close_status'] == 0,
                'candidate input or lifecycle differs')
        if expected['operation'] == 'read':
            returned = original['result']
            raw = bytes(returned['bytes'])
            require(returned['length'] == len(raw) and raw == value.encode('utf-8')
                    and returned['value'] == candidate['value'] == value, 'original/candidate read bytes differ')
        else:
            require(original['input'] == value
                    and original['return_type'] == 'void; raw register not interpreted as status',
                    'original write value or status interpretation differs')
        require([item['value'] for item in row['after']] == candidate['disk'] == after,
                'original state or closed candidate disk differs')
        for index, item in enumerate(row['after']):
            require(bool(item.get('encoded')) == bool(after[index]), 'original encrypted presence differs')
            if not (changed & (1 << index)):
                require(item == row['before'][index], 'unchanged original raw entry mutated')
        events = delta(row['events_before'], row['events_after'])
        require(len({event['thread'] for event in events}) == 1, 'sequential window thread attribution differs')
        commits = {store + ':true': 1 for index, store in enumerate(['o', 'p']) if changed & (1 << index)}
        require(commit_counts(events) == commits, 'actual delegated commits differ')
        require(candidate['receipt'] == dict(changed=changed, committed=changed, failed=0, renamed=changed,
                error_o=0, error_p=0, backup_requested=int(changed != 0)), 'candidate commit receipt differs')
        summaries.append(dict(case=row['case'], operation=expected['operation'],
                              commits=commits, equal_state=True, equal_readback=expected['operation'] == 'read'))
    return dict(cases=len(rows), reads=7, writes=4, raw_entries_restored=True,
                priority_and_backfill='verified for explicit original native bridge matrix', rows=summaries,
                backup_delivery='unknown', concurrency_equivalence='unknown')


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1f-private-manifest.v1', 'wrong manifest schema')
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
    hashes = {name: hashlib.sha256(raw(item['path'])).hexdigest() for name, item in artifacts.items()}
    require(hashes['library'] == 'd7000052d6b965364cbaced93b4aa59c916ceb29a011d21f9c1f10202264d6b5',
            'previous candidate binary identity differs')
    build = load('build-reproduction.json')
    require(build['exit_code'] == 0 and build['artifact_sha256'] == build['rebuilt_sha256'] == hashes['library']
            and all(flag in build['argv'] for flag in ['-shared', '-fPIC', '-Werror'])
            and build['argv'][0].endswith('/aarch64-linux-android30-clang'), 'native build not reproduced')
    for name in ['e2_1_pn_store.c', 'e2_1_pn_store.h']:
        require(build['sources'][name] == manifest['files'][manifest['verification_sources'][name]],
                'compiled candidate source differs')
    commands = load('observer-build/commands.json')
    require(len(commands) == 10 and all(row['exit_code'] == 0 for row in commands)
            and commands[0]['argv'][0].endswith('/javac')
            and commands[1]['argv'][0].endswith('/d8'), 'observer build/upload failed')
    for name in artifacts:
        actual = raw('observer-build/' + ('candidate' if name == 'library' else 'dex') + '-hash.stdout').decode().split()
        require(actual == [hashes[name], artifacts[name]['guest']], 'guest artifact hash not observed')
    baseline = RUNS[0]
    original = load(baseline + '/identity-before.json')
    require(len(original['files']) == 7, 'baseline identity set differs')
    processes, results, matrices = set(), [], []
    for run in RUNS:
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        require(config['mode'] == 'original' and 'replacement' not in config, 'original bridge must remain installed')
        messages = [json.loads(line) for line in raw(prefix + 'messages.jsonl').splitlines() if line.strip()]
        completed_run(base, run, load(prefix + 'run-finished.json'), messages)
        process = load(prefix + 'owned-process.json')
        require(process['pid'] not in processes, 'independent process missing')
        processes.add(process['pid'])
        identity = load(prefix + 'identity-before.json')
        require(identity == load(prefix + 'identity-after.json'), 'frozen inputs changed during run')
        expected = dict(original['files'])
        if run != baseline:
            expected.update({item['guest']: hashes[name] for name, item in artifacts.items()})
            require(config['pn_matrix']['library'] == artifacts['library']['guest']
                    and config['return_observer']['dex_path'] == artifacts['dex']['guest']
                    and config['return_observer']['enable_pn_matrix'] is True, 'executed artifact identity differs')
            for name in ['E21PreferenceObserver.java', 'e2_1_pn_matrix.py', 'e2_1_pn_store.c', 'e2_1_pn_store.h']:
                require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                        'exercised source differs: ' + name)
            require(load(prefix + 'return-observer-uninstall.json')['result'] == 'uninstalled\t2'
                    and load(prefix + 'return-observer-release.json')['result'] == 'released\t6',
                    'observer not restored and retired')
            observer = load(prefix + 'return-observer-final.json')
            require(observer['references'] == dict(allocated_globals=1, deleted_globals=1, retired=True),
                    'owned global reference not retired')
            matrices.append(dict(run=run, **validate_matrix(load(prefix + 'pn-matrix.json'),
                load(prefix + 'pn-matrix-candidate-final.json'))))
        else:
            require('pn_matrix' not in config and 'return_observer' not in config, 'baseline contains PN mutation')
        require(identity['files'] == config['guest_identity'] == expected
                and identity['update_manifest_sha256'] == config['update_identity_sha256']
                == original['update_manifest_sha256'], 'original inputs or update identity differ')
        commands = [json.loads(line) for line in raw(prefix + 'commands.jsonl').splitlines() if line.strip()]
        updates = [row for row in commands if row['argv'][-1].startswith('find ')
                   and 'update -type f -exec sha256sum' in row['argv'][-1]]
        require(len(updates) == 2 and all(row['code'] == 0 and len(row['stdout'].splitlines()) == 990
                and hashlib.sha256(row['stdout'].strip().encode()).hexdigest() == identity['update_manifest_sha256']
                for row in updates), '990-file before/after update identity missing')
        require(raw(prefix + 'sources/run_e2_observed.py') == raw(manifest['verification_sources']['run_e2_observed.py'])
                and load(prefix + 'run-start.json')['runner_sha256'] == manifest['files'][prefix + 'sources/run_e2_observed.py'],
                'runner identity differs')
        for name, digest in load(prefix + 'source-identity.json').items():
            require(manifest['files'].get(prefix + 'sources/' + name) == digest, 'archived script identity differs')
        for name in ['e2_1_return_observer.js', 'e2_1_config_probe.js', 'e2_1_pn_matrix_candidate.js']:
            require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                    'exercised JavaScript source differs: ' + name)
        for tick in range(3602):
            raw(prefix + f'snapshots/tick-{tick:06d}.bin')
        comparison = verify(base / baseline / 'snapshots', base / run / 'snapshots',
                            load(baseline + '/trace.json'), load(prefix + 'trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'native trajectory differs')
        require(controls(load(prefix + 'control-receipts.json')) == controls(load(baseline + '/control-receipts.json')),
                'native action/snapshot/restore/reset/terminal differs')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        results.append(dict(run=run, **{key: comparison[key] for key in EXPECTED_TRAJECTORY},
                            platform_asset_events=0, provider_failures=0, live_assets=0))
    return dict(schema='e2.1f-bounded-summary.v1', status='bounded_evidence_verified', strict_gate='BLOCKED',
                attachment_count=len(manifest['files']), runs=results, matrices=matrices,
                scope='post-trajectory explicit original bridge; independent logical-state native candidate',
                pn_native_replacement=False, original_storage_format_compatibility=False,
                no_vm_verified=False, android_free_verified=False, training_admissible=False,
                tenfold_speedup_verified=False, removed_keys=0,
                unresolved=['native bridge integration and original Cipher/XML storage handoff',
                            'backup delivery', 'concurrency equivalence and crash durability', '46-key removability'])


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
    print(json.dumps(dict(status=result['status'], strict_gate=result['strict_gate'],
                          attachment_count=result['attachment_count'])))


if __name__ == '__main__':
    main()
