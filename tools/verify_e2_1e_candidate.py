"""Verify isolated PN candidate evidence without admitting a bridge replacement."""

import argparse
import hashlib
import json
from pathlib import Path

from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources


SOURCES = ['e2_1_pn_store.c', 'e2_1_pn_store.h', 'e2_1_pn_candidate.js', 'run_e2_observed.py',
           'verify_e2_1e_candidate.py', 'verify_e0_snapshots.py', 'verify_e1_gate.py', 'verify_e2_gate.py']
CASES = [('delete', ''), ('normal', 'pn-controlled-19'), ('same', 'pn-controlled-19'),
         ('malformed', 'opaque-not-decimal'), ('unicode-bmp', '中文'), ('longest', 'x' * 4095), ('empty', '')]


def check_receipt(result, changed=0, failed=0):
    require(result['status'] == 0, 'candidate ABI status differs')
    expected = dict(changed=changed, committed=changed & ~failed, failed=failed,
                    renamed=changed & ~failed, error_o=13 if failed & 1 else 0,
                    error_p=13 if failed & 2 else 0, backup_requested=int(changed != 0))
    require(result['receipt'] == expected, 'candidate commit receipt differs')


def original_read(row):
    result = row['result']
    raw = bytes(result['bytes'])
    require(result['length'] == len(raw) and raw.decode('utf-8') == result['value'],
            'original return bytes differ')
    return result['value']


def validate_replay(originals, replay):
    require(replay['schema'] == 'e2.1-pn-candidate-replay.v1' and replay['closed'] is True,
            'candidate not closed or wrong schema')
    require(replay['replacement_verified'] is False and replay['no_vm_verified'] is False
            and replay['original_storage_format_compatibility'] is False
            and replay['concurrency_equivalence'] == 'unknown'
            and replay['backup_delivery'] == 'unknown; receipt records intent only', 'unsupported promotion')
    expected = [('saved', 'read')]
    for name, _ in CASES:
        expected.extend([(name, 'write'), (name + '-read', 'read')])
    expected.extend([('restore-saved', 'write'), ('verify-restored', 'read')])
    require([(row['case'], row['operation']) for row in originals] == expected, 'original case order differs')
    for row in originals:
        require(row['key'] == 'PN_POPUP' and row['exception_state']['before'] is False
                and row['exception_state']['after'] is False, 'original key or JNI exception differs')
        require(row['entry_offset'] == (21022348 if row['operation'] == 'read' else 21018548),
                'original bridge provenance differs')
    saved = original_read(originals[0])
    require(originals[-2]['input'] == original_read(originals[-1]) == saved, 'original value not restored')
    rows = replay['results']
    require([(row['case'], row['operation']) for row in rows] == expected[1:-2], 'candidate case order differs')
    before = ''
    for index, (name, value) in enumerate(CASES):
        write, read = originals[1 + 2 * index:3 + 2 * index]
        candidate_write, candidate_read = rows[2 * index:2 * index + 2]
        require(write['input'] == value and original_read(read) == value, 'original controlled value differs')
        check_receipt(candidate_write['candidate'], 3 if before != value else 0)
        check_receipt(candidate_read['candidate'])
        require(candidate_read['candidate']['value'] == candidate_read['original_value'] == value
                and candidate_read['equal'] is True, 'candidate read differs from original bytes')
        for original, candidate in [(write, candidate_write), (read, candidate_read)]:
            require(candidate['original_exception_state'] == original['exception_state'], 'exception evidence differs')
        before = value
    experiments = replay['experiments']
    require([row['case'] for row in experiments] == ['close-rejects-read', 'reopen', 'durable-setup',
            'real-permission-failure', 'close-rejects-read', 'reopen-after-failure', 'close-rejects-read'],
            'candidate lifecycle experiment order differs')
    for row in experiments:
        if row['case'] == 'close-rejects-read':
            require(row['status'] == -2, 'closed candidate accepted read')
    reopened, setup, fault, persisted = [experiments[index] for index in (1, 2, 3, 5)]
    for result in [reopened['before'], reopened['after'], setup['observed'], fault['visible'], persisted['observed']]:
        check_receipt(result)
    require(reopened['before']['value'] == reopened['after']['value'] == '' and reopened['equal'] is True,
            'normal reopen differs')
    check_receipt(setup['result'], 3)
    require(setup['observed']['value'] == 'candidate-durable', 'durable setup not observed')
    check_receipt(fault['failed'], 3, 3)
    check_receipt(fault['same'])
    require(fault['visible']['value'] == 'candidate-volatile', 'failed write not visible in memory')
    require(persisted['observed']['value'] == 'candidate-durable' and persisted['equal'] is True,
            'failed write persisted or reopen missing')
    return dict(original_rows=17, candidate_rows=14, equal_readbacks=7, close_rejections=3,
                real_permission_failure=True, same_value_no_retry=True, normal_reopen_equal=True,
                failed_write_not_persisted=True)


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1e-private-manifest.v1', 'wrong evidence schema')
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
    artifact = manifest['artifact']
    artifact_hash = hashlib.sha256(raw(artifact['path'])).hexdigest()
    build = load('build-reproduction.json')
    require(build['exit_code'] == 0 and build['artifact_sha256'] == build['rebuilt_sha256'] == artifact_hash
            and build['argv'][0].endswith('/aarch64-linux-android30-clang')
            and all(flag in build['argv'] for flag in ['-shared', '-fPIC', '-Werror']), 'candidate build not reproduced')
    require(all(build['sources'][name] == manifest['files'][manifest['verification_sources'][name]]
                for name in ['e2_1_pn_store.c', 'e2_1_pn_store.h']), 'compiled source identity differs')
    install = load('candidate-2-install.json')
    require(len(install) == 6 and all(row['exit_code'] == 0 for row in install)
            and install[1]['argv'][-3:] == ['push', build['argv'][-1].replace('-rebuilt', ''), artifact['guest']]
            and install[4]['stdout'].split() == [artifact_hash, artifact['guest']], 'candidate install not verified')
    baseline, candidate = manifest['baseline'], manifest['candidate']
    require((baseline, candidate) == ('baseline', 'candidate-3'), 'bounded run set differs')
    original = load(baseline + '/identity-before.json')
    require(len(original['files']) == 7, 'original input set differs')
    processes, results = set(), []
    for run in [baseline, candidate]:
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        require(config['mode'] == 'original' and 'replacement' not in config, 'original bridge must remain installed')
        messages = [json.loads(line) for line in raw(prefix + 'messages.jsonl').splitlines() if line.strip()]
        require(not any(row.get('type') == 'error' for row in messages), 'Frida runtime error')
        completed_run(base, run, load(prefix + 'run-finished.json'), messages)
        process = load(prefix + 'owned-process.json')
        require(process['pid'] not in processes, 'independent process missing')
        processes.add(process['pid'])
        identity = load(prefix + 'identity-before.json')
        require(identity == load(prefix + 'identity-after.json'), 'inputs changed during native run')
        expected = dict(original['files'])
        if run == candidate:
            expected[artifact['guest']] = artifact_hash
            require(config['pn_candidate']['library'] == artifact['guest'], 'loaded candidate identity differs')
            for name in ['e2_1_pn_store.c', 'e2_1_pn_store.h', 'e2_1_pn_candidate.js']:
                require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                        'candidate source not exercised: ' + name)
        else:
            require('pn_candidate' not in config, 'baseline contains candidate')
        require(identity['files'] == config['guest_identity'] == expected
                and identity['update_manifest_sha256'] == config['update_identity_sha256']
                == original['update_manifest_sha256'], 'frozen inputs or update identity differ')
        require(raw(prefix + 'sources/run_e2_observed.py') == raw(manifest['verification_sources']['run_e2_observed.py'])
                and load(prefix + 'run-start.json')['runner_sha256'] == manifest['files'][prefix + 'sources/run_e2_observed.py'],
                'runner source identity differs')
        for name, digest in load(prefix + 'source-identity.json').items():
            require(manifest['files'].get(prefix + 'sources/' + name) == digest, 'script archive identity differs')
        for tick in range(3602):
            raw(prefix + f'snapshots/tick-{tick:06d}.bin')
        comparison = verify(base / baseline / 'snapshots', base / run / 'snapshots',
                            load(baseline + '/trace.json'), load(prefix + 'trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'native trajectory differs')
        require(controls(load(prefix + 'control-receipts.json')) == controls(load(baseline + '/control-receipts.json')),
                'snapshot/restore/reset/action/terminal lifecycle differs')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        results.append(dict(run=run, **{key: comparison[key] for key in EXPECTED_TRAJECTORY},
                            platform_asset_events=0, provider_failures=0, live_assets=0))
    semantics = validate_replay(load(candidate + '/semantics-cases.json'), load(candidate + '/pn-candidate-replay.json'))
    require(load(candidate + '/pn-candidate-replay.json') == load(candidate + '/pn-candidate-final.json'),
            'final candidate state differs')
    failure = load('candidate-1/run-finished.json')
    require(failure['status'] == 'failed' and failure['exit_code'] == 1
            and 'Permission denied' in failure['error'], 'initial load failure not retained')
    return dict(schema='e2.1e-bounded-summary.v1', status='bounded_evidence_verified', strict_gate='BLOCKED',
                attachment_count=len(manifest['files']), runs=results, semantics=semantics,
                scope='post-trajectory isolated candidate; original JNI bridge never replaced',
                pn_native_replacement=False, no_vm_verified=False, android_free_verified=False,
                training_admissible=False, tenfold_speedup_verified=False, removed_keys=0,
                unresolved=['native bridge integration', 'original Cipher/XML storage migration',
                            'real original p/o conflict and backfill differential', 'backup delivery',
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
    print(json.dumps(dict(status=result['status'], strict_gate=result['strict_gate'],
                          attachment_count=result['attachment_count'])))


if __name__ == '__main__':
    main()
