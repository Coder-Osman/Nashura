"""Verify a bounded 32-key replacement, never infer no-VM or Android-free."""

import argparse
import hashlib
import json
from pathlib import Path

from verify_e0_snapshots import summarize_counts, verify
from verify_e1_gate import DESTROY_LABELS, EXPECTED_TRAJECTORY, RESOURCE_IMPORTS


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def controls(rows):
    result = []
    for row in rows:
        if row['command'] == 'status':
            continue
        response = dict(row['response'])
        require(response.get('ok') is True, 'native control rejected')
        if row['phase'] == 'full_trace':
            response.pop('snapshot_directory', None)
        result.append(dict(command=row['command'], phase=row['phase'], response=response))
    require(len(result) == 17, 'complete native command lifecycle missing')
    require(result[-2]['phase'] == 'terminal_reset_destroy', 'terminal reset missing')
    require(result[-1]['phase'] == 'final_reset_state', 'final state missing')
    return result


def resources(counts, redirect, fmod):
    summary = summarize_counts(counts)
    require(RESOURCE_IMPORTS <= summary['labels'].keys(), 'platform resource coverage missing')
    for name in RESOURCE_IMPORTS:
        row = summary['labels'][name]
        require(row['attached'] and not row['failed'] and row['count'] == 0,
                'platform AssetManager event or uncovered slot: ' + name)
    require(not any(row['count'] for row in counts['counts']
                    if any(label.startswith('AAsset') for label in row['labels'])),
            'actual AssetManager events remain')
    destroyed = {name for row in counts['counts'] if row.get('host_phase') == 'terminal_reset_destroy'
                 and row['count'] > 0 for name in row['labels'] if name in row.get('native_scopes', [])}
    require(DESTROY_LABELS <= destroyed, 'native destroy evidence missing')
    receipt, metrics = redirect['receipt'], redirect['metrics']
    require(receipt['installed'] is True and receipt['errors'] == [], 'resource redirect failed')
    require(len(receipt['redirects']) == 9 and {row['name'] for row in receipt['redirects']} == RESOURCE_IMPORTS
            and all(row['verified'] is True for row in receipt['redirects']), 'nine imports not verified')
    require(metrics['failures'] == '0' and metrics['live_assets'] == '0' and metrics['entries'] == '9829'
            and int(metrics['calls']) > 0, 'provider failure or live assets')
    redirected = [row for row in fmod['events'] if row['kind'] == 'redirected_binding']
    expected = {'AAssetManager_fromJava', 'AAssetManager_open', 'AAsset_read', 'AAsset_close', 'AAsset_getLength', 'AAsset_seek'}
    require(fmod['installed'] is True and fmod['errors'] == [] and len(redirected) == 6
            and {row['name'] for row in redirected} == expected
            and all(row['verified'] is True and row['target'].startswith('libe1provider.so+') for row in redirected),
            'FMOD provider binding incomplete')


def replacement(receipt, observer, original, config):
    require(receipt['schema'] == 'e2-config-replacement.v1', 'wrong replacement schema')
    mode = config['mode']
    require(receipt['mode'] == mode, 'mode mismatch')
    status = receipt['status']
    require(status['installed'] is True and receipt['errors'] == [], 'replacement errors')
    require(status['mismatches'] == 0 and status['targeted_fallback'] == 0, 'targeted mismatch or fallback')
    require(receipt['store']['failures'] == '0', 'native store failure')
    entries = config['replacement']['entries']
    excluded = config['replacement']['excluded_keys']
    require(len(entries) == 32 and not (entries.keys() & excluded.keys()), 'target scope changed')
    observed = {row['key']['value']: row for row in original['config_calls']
                if row['operation'] == 'PreferenceNativeReadBridge' and row['phase'] == 'configuration'
                and row['key']['status'] == 'decoded' and row['returned_string']['status'] == 'decoded'}
    for key, entry in entries.items():
        require(key in observed and entry['value'] == observed[key]['returned_string']['value'], 'contract lacks original return evidence')
    require(all(excluded[key].get('reason') and excluded[key].get('evidence') for key in excluded), 'unexplained exclusion')
    source_rows = {row['sequence']: row for row in original['config_calls']}
    for key, exclusion in excluded.items():
        require(all(sequence in source_rows and source_rows[sequence]['key'].get('value') == key
                    for sequence in exclusion['evidence']), 'exclusion does not match original evidence')
    events = receipt['events']
    require(all(row.get('targeted') is (row.get('key') in entries) for row in events), 'target attribution differs from declared contract')
    targeted = [row for row in events if row.get('targeted') is True]
    read_rows = [row for row in targeted if row['operation'] == 'read']
    write_rows = [row for row in targeted if row['operation'] == 'write']
    require(len(read_rows) == status['targeted_reads'] == 32 and len(write_rows) == status['targeted_writes'] == 4,
            'target event counts inconsistent')
    require({row['key'] for row in read_rows} == entries.keys(), 'declared keys not exercised')
    require({row['key'] for row in write_rows} == {key for key, entry in entries.items() if entry['writable']}, 'declared writes not exercised')
    require(receipt['store']['reads'] == str(len(read_rows)) and receipt['store']['writes'] == str(len(write_rows)), 'native store counters differ')
    for row in read_rows:
        require(row['expected'] == row['actual'] == entries[row['key']]['value'], 'replacement read differs')
        require(row['route'] == ('shadow_read_original' if mode == 'shadow' else 'native_read'), 'wrong read route')
    for row in write_rows:
        require(row['value'] == entries[row['key']]['value'], 'write evidence changed')
        require(row['route'] == ('shadow_write_original' if mode == 'shadow' else 'native_write'), 'wrong write route')
    fallback = [row for row in events if row['route'] == 'original_fallback']
    require(len(fallback) == status['fallback'], 'fallback count differs')
    require(all(row['key'] in excluded and row.get('exclusion') for row in fallback), 'unlisted fallback')
    for row in events:
        require(type(row.get('thread')) is int and row.get('phase') and row.get('caller') and row['caller'] != 'unknown' and not row.get('error'),
                'missing event provenance')
    require(observer['errors'] == [] and observer['dropped']['errors'] == 0 and observer['dropped']['config_calls'] == 0,
            'observer failure or lost config rows')
    if mode != 'shadow':
        require(not any(row['operation'] == 'PreferenceJNIReadBridge' and row['key'].get('value') in entries
                        for row in observer['config_calls']), 'declared read still reaches JNI bridge')
    return dict(targeted_reads=len(read_rows), targeted_writes=len(write_rows),
                explicitly_excluded_fallbacks=len(fallback), targeted_fallback=0)


def completed_run(base, run, finished, messages):
    require(not (base / run / 'evidence-incomplete.json').exists(), 'final evidence incomplete: ' + run)
    require(finished['status'] == 'completed' and finished['exit_code'] == 0, 'run failed: ' + run)
    require(not any(message.get('payload', {}).get('type') == 'error' for message in messages),
            'Frida script error in completed run: ' + run)


def evaluate(manifest, base, scope='strict'):
    require(manifest.get('schema') == 'e2-private-evidence-manifest.v1', 'wrong evidence schema')
    require(scope in {'strict', 'fixed-trajectory'}, 'unknown acceptance scope')
    attachments = {}
    def read(relative):
        expected = manifest['files'][relative]
        path = Path(relative)
        require(not path.is_absolute() and '..' not in path.parts, 'unsafe attachment path')
        raw = (base / path).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == expected, 'attachment hash differs: ' + relative)
        attachments[relative] = expected
        return raw
    def load(relative):
        return json.loads(read(relative))
    baseline = manifest['original']
    original = load(baseline + '/e2-final.json')
    require(original['errors'] == [] and original['dropped']['config_calls'] == 0, 'incomplete original observations')
    original_trace = load(baseline + '/trace.json')
    original_controls = controls(load(baseline + '/control-receipts.json'))
    original_contract = None
    results = []
    processes = set()
    modes = []
    for run in [baseline] + manifest['runs']:
        config = load(run + '/private-config.json')
        finished = load(run + '/run-finished.json')
        messages = [json.loads(line) for line in read(run + '/messages.jsonl').splitlines() if line.strip()]
        completed_run(base, run, finished, messages)
        owned = load(run + '/owned-process.json')
        require(owned['pid'] not in processes, 'duplicate process evidence')
        processes.add(owned['pid'])
        before, after = load(run + '/identity-before.json'), load(run + '/identity-after.json')
        require(before == after and before['files'] == config['guest_identity']
                and before['update_manifest_sha256'] == config['update_identity_sha256'], 'original input identity changed')
        require(all(before['files'].get(path) == value for path, value in manifest['required_guest_identity'].items()), 'frozen engine/resource identity mismatch')
        require(before['update_manifest_sha256'] == manifest['update_manifest_sha256'], 'update identity changed across runs')
        trace = load(run + '/trace.json')
        comparison = verify(base / baseline / 'snapshots', base / run / 'snapshots', original_trace, trace)
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'unexpected complete trajectory')
        require(controls(load(run + '/control-receipts.json')) == original_controls, 'native action/reset/restore response differs')
        resources(load(run + '/e0-final.json'), load(run + '/resource-final.json'), load(run + '/fmod-final.json'))
        row = dict(run=run, mode=config['mode'], snapshot_count=3602, trajectory_sha256=comparison['trajectory_sha256'])
        if run != baseline:
            current_contract = dict(entries=config['replacement']['entries'], excluded_keys=config['replacement']['excluded_keys'])
            if original_contract is None:
                original_contract = current_contract
            require(current_contract == original_contract, 'contract changes between required modes')
            row.update(replacement(load(run + '/replacement-final.json'), load(run + '/e2-final.json'), original, config))
        results.append(row)
        modes.append(config['mode'])
    require(modes[0] == 'original' and modes.count('shadow') >= 1 and modes.count('replacement') >= 3
            and modes.count('strict-targeted') >= 2, 'independent required modes missing')
    require(set(manifest['negative_native']) == {'omitted_key', 'shadow_mismatch'}, 'native negative experiments missing')
    for kind, directory in manifest['negative_native'].items():
        finished = load(directory + '/run-finished.json')
        require(finished['status'] == 'failed' and finished['exit_code'] != 0, 'negative native attempt accepted')
        violation = load(directory + '/violation.json')
        require(violation['event'] == 'e2_contract_violation' and violation['detail']['error'], 'missing native violation evidence')
    strict_gaps = ['native default/missing-value equivalence beyond observed empty strings',
                   'native configuration exception equivalence',
                   'native repeated initialization and persistence equivalence',
                   'native store close/free lifecycle and reference retirement']
    return dict(schema='e2-acceptance.v2', status='passed' if scope == 'fixed-trajectory' else 'blocked',
                requested_scope=scope, fixed_trajectory_check='passed', targeted_strict_gate_verified=False,
                strict_gate_gaps=strict_gaps,
                scope='32 declared stable preference keys only; individually enumerated fallback remains outside gate',
                runs=results, attachment_sha256=attachments, targeted_config_verified=True,
                whole_e2_closure_verified=False,
                semantics_scope='observed fixed-lifecycle string reads, empty results, four idempotent writes, reset and two native negative experiments; generalized defaults/persistence/reinitialization unknown',
                no_vm_verified=False, android_free_verified=False, training_admissible=False, tenfold_speedup_verified=False,
                uncovered=['unknown Java class/reference lifetime', 'unsampled JNI return values',
                           'unconfigured indirect targets', 'OS teardown', 'full configuration persistence', 'natural FMOD playback'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scope', choices=['strict', 'fixed-trajectory'], default='strict')
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(args.manifest.read_text()), args.manifest.parent, args.scope)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as failure:
        result = dict(schema='e2-acceptance.v2', status='blocked', reasons=[str(failure)], targeted_config_verified=False,
                      targeted_strict_gate_verified=False, whole_e2_closure_verified=False,
                      no_vm_verified=False, android_free_verified=False, training_admissible=False, tenfold_speedup_verified=False)
    with args.output.open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'attachment_sha256'}))
    raise SystemExit(0 if result['status'] == 'passed' else 2)


if __name__ == '__main__':
    main()
