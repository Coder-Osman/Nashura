"""Verify bounded E2.1-B evidence, without promoting the strict or no-VM gates."""

import argparse
import hashlib
import json
from pathlib import Path

from e2_1_method_trace import parse
from e2_1_removability import build
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources
from verify_e2_1_gate import replacement_events


CASES = ['normal_repeat', 'changed_write', 'changed_read', 'same_write',
         'empty_write', 'empty_read', 'restore', 'restored_read']


def call_count(root, suffix):
    return sum(row['method'].endswith(suffix) for row in root['selected_calls'])


def sequential(probes, trace, prepare=False):
    expected = CASES + (['prepare_fresh', 'prepared_read'] if prepare else [])
    require([row['case'] for row in probes] == expected, 'PN probe sequence differs')
    require(len({row['thread'] for row in probes}) == 1, 'sequential native probe threads differ')
    roots = trace['roots']
    require(len(roots) == len(probes) and len({row['thread'] for row in roots}) == 1,
            'Java roots do not match sequential probe window')
    require(not any('.apply(' in name for name in trace['selected_counts']), 'unexpected apply path')
    cases = {row['case']: row for row in probes}
    require(cases['changed_write']['input'] != cases['normal_repeat']['result']['value'],
            'changed-write case was not a change')
    require(cases['changed_read']['result']['value'] == cases['changed_write']['input']
            == cases['same_write']['input'], 'changed/same input or read differs')
    require(cases['empty_write']['input'] == cases['empty_read']['result']['value'] == '',
            'empty write/read differs')
    require(cases['restore']['input'] == cases['restored_read']['result']['value'], 'restoration differs')
    if prepare:
        require(cases['prepare_fresh']['input'] == cases['prepared_read']['result']['value'],
                'fresh process preparation differs')
    summaries = []
    for probe, root in zip(probes, roots):
        require(probe['exception_state']['before'] is False and probe['exception_state']['after'] is False,
                'pending probe exception')
        require(root['completion'] == 'returned' and root['method'] ==
                ('getKeyValue' if probe['operation'] == 'read' else 'storeKeyValue'), 'Java root mismatch')
        expected_commits = 2 if probe['case'] in {'changed_write', 'empty_write', 'restore', 'prepare_fresh'} else 0
        for suffix in ['.commit()Z', '.commitToMemory()Landroid/app/SharedPreferencesImpl$MemoryCommitResult;',
                       '.writeToFile(Landroid/app/SharedPreferencesImpl$MemoryCommitResult;Z)V']:
            require(call_count(root, suffix) == expected_commits, 'commit/writeToFile path differs')
        summaries.append(dict(case=probe['case'], operation=probe['operation'], commits=expected_commits,
                              completion=root['completion']))
    return dict(cases=summaries, trace_sha256=trace['trace_sha256'], records=trace['records'],
                commit_return_value='unknown', power_loss_durability='unknown',
                mapping='single ordered explicit window; ART thread IDs are not Linux TIDs')


def concurrent(result, trace, values):
    workers = result['workers']
    require(result['status'] == 0 and len(workers) == 2
            and len({row['thread'] for row in workers}) == 2
            and all(row['completed'] == 4 and row['error'] == 0 and row['detached'] is True
                    for row in workers), 'concurrent native worker failed')
    require(values['same_value_after'] is True and values['before_sha256'] == values['after_sha256'],
            'same-value concurrent probe changed PN')
    roots = trace['roots']
    require(len(roots) == 16 and len({row['thread'] for row in roots}) == 2,
            'concurrent Java root coverage differs')
    for thread in {row['thread'] for row in roots}:
        sequence = [row['method'] for row in roots if row['thread'] == thread]
        require(sequence == ['storeKeyValue', 'getKeyValue'] * 4, 'concurrent Java worker sequence differs')
    require(all(row['completion'] == 'returned' and call_count(row, '.commit()Z') == 0
                for row in roots), 'same-value concurrent commit or unwind')
    require(all(row['end_wall'] >= row['start_wall'] for row in roots), 'wall clock wrap unsupported')
    overlaps = sum(first['thread'] != second['thread']
                   and max(first['start_wall'], second['start_wall']) < min(first['end_wall'], second['end_wall'])
                   for index, first in enumerate(roots) for second in roots[index + 1:])
    require(overlaps > 0, 'concurrent windows did not overlap')
    return dict(workers=2, write_calls=8, read_calls=8, commit_calls=0,
                overlapping_java_root_pairs=overlaps, trace_sha256=trace['trace_sha256'],
                scope='same saved value only; competing values and general thread safety unknown',
                thread_mapping='two native workers and two ART streams; numerical IDs not equated')


def fresh_transition(observer, receipt, prepared):
    wrappers = [row for row in observer['config_calls'] if row['key'].get('value') == 'PN_POPUP'
                and row['operation'] in {'PreferenceRead', 'PreferenceWrite'}]
    require(len(wrappers) == 2 and [row['operation'] for row in wrappers] ==
            ['PreferenceRead', 'PreferenceWrite'], 'fresh natural wrapper read/write absent')
    read, write = wrappers
    require(read['returned_string']['status'] == write['value']['status'] == 'decoded'
            and read['returned_string']['value'] == prepared
            and write['value']['value'] == str(int(prepared) + 1), 'fresh process transition differs')
    require(read['thread'] == write['thread'] and read['sequence'] < write['sequence'],
            'fresh transition order/thread differs')
    calls = [row for row in receipt['events'] if row['key'] == 'PN_POPUP'
             and row['native_scope'] is not None]
    require(len(calls) == 2 and [row['operation'] for row in calls] == ['read', 'write']
            and all(row['thread'] == read['thread'] and row['route'] == 'original_fallback'
                    and row['targeted'] is False for row in calls), 'fresh fallback route differs')
    return dict(status='verified', value_layer='original top-level PreferenceRead/Write wrappers',
                route_layer='two matching-thread fallback dispatches inside original constructor',
                cross_observer_sequence_alignment='not inferred')


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1b-private-manifest.v1', 'wrong bounded evidence schema')
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

    for filename in ['verify_e2_1b_evidence.py', 'e2_1_method_trace.py', 'e2_1_removability.py']:
        require(raw(manifest['verification_sources'][filename]) == Path(__file__).with_name(filename).read_bytes(),
                'active bounded verifier source differs')

    baseline = manifest['baseline']
    reference = manifest['historical_reference']
    original_identity = load(baseline + '/identity-before.json')
    historical = verify(base / reference / 'snapshots', base / baseline / 'snapshots',
                        load(reference + '/trace.json'), load(baseline + '/trace.json'))
    require(all(historical[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'historical trajectory differs')
    require(load(reference + '/identity-before.json') == original_identity, 'historical input identity differs')
    runs = manifest['runs']
    require(len(set(runs)) == len(runs) == 5 and runs[0] == baseline, 'five unique runs required')
    rows = []
    processes = set()
    modes = []
    previous_prepared = None
    for index, run in enumerate(runs):
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        messages = [json.loads(line) for line in raw(prefix + 'messages.jsonl').splitlines() if line.strip()]
        completed_run(base, run, load(prefix + 'run-finished.json'), messages)
        require(load(prefix + 'identity-before.json') == load(prefix + 'identity-after.json') == original_identity,
                'input identity changed')
        require(original_identity['files'] == config['guest_identity']
                and original_identity['update_manifest_sha256'] == config['update_identity_sha256'], 'config identity differs')
        start = load(prefix + 'run-start.json')
        require(manifest['files'][manifest['configs'][run]] == start['config_sha256']
                and load(manifest['configs'][run]) == config, 'config source identity differs')
        require(manifest['files'][prefix + 'sources/run_e2_observed.py'] == start['runner_sha256'], 'runner identity differs')
        for filename, digest in load(prefix + 'source-identity.json').items():
            require(manifest['files'].get(prefix + 'sources/' + filename) == digest, 'source identity differs')
        pid = load(prefix + 'owned-process.json')['pid']
        require(pid not in processes, 'native process reused')
        processes.add(pid)
        for tick in range(3602):
            for directory in {run, reference, baseline}:
                require(directory + '/snapshots/tick-' + str(tick).zfill(6) + '.bin' in manifest['files'], 'unbound snapshot')
        comparison = verify(base / baseline / 'snapshots', base / run / 'snapshots',
                            load(baseline + '/trace.json'), load(prefix + 'trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'trajectory differs')
        require(controls(load(prefix + 'control-receipts.json')) == controls(load(baseline + '/control-receipts.json')),
                'native lifecycle controls differ')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        observer = load(prefix + 'e2-final.json')
        require(observer['errors'] == [] and observer['dropped']['config_calls'] == observer['dropped']['errors'] == 0,
                'configuration evidence incomplete')
        row = dict(run=run, mode=config['mode'], comparison=comparison,
                   resource_platform_events=0, provider_failures=0, live_assets=0,
                   jni_samples_dropped=observer['dropped']['jni_samples'], exhaustive_jni_arguments=False)
        if index:
            oracle = load(prefix + 'java-oracle-final.json')
            require(oracle['active'] is False and [event['operation'] for event in oracle['events']] ==
                    ['startMethodTracing', 'stopMethodTracing'] and all(event['status'] ==
                    'returned_without_pending_exception' for event in oracle['events']), 'oracle incomplete')
            probes = load(prefix + 'semantics-cases.json')
            row['pn_sequence'] = sequential(probes, parse(raw(prefix + 'java-methods.trace')), prepare=index == 1)
            if index == 1:
                previous_prepared = probes[-2]['input']
            if index == 2:
                row['cross_process_prepared_read_and_increment'] = fresh_transition(
                    observer, load(prefix + 'replacement-final.json'), previous_prepared)
            if 'replacement' in config:
                receipt = load(prefix + 'replacement-final.json')
                row.update(replacement_events(receipt, config))
                require(row['fallback_calls_including_probes'] == receipt['status']['fallback'], 'fallback counter differs')
                require('PN_POPUP' not in config['replacement']['entries'], 'PN replacement outside scope')
                require(config['replacement']['entries'] == load(runs[2] + '/private-config.json')['replacement']['entries'],
                        '32-key contract changed between modes')
        modes.append(config['mode'])
        rows.append(row)
    require(modes == ['original', 'original', 'shadow', 'replacement', 'strict-targeted'], 'four-mode series missing')
    explicit = [row for name in ['semantics-cold', 'semantics-reset', 'semantics-terminal-reset']
                for row in load(baseline + '/' + name + '.json')]
    ledger = build(load(manifest['ledger']), load(baseline + '/e2-final.json'), explicit,
                   dict(observer=manifest['files'][baseline + '/e2-final.json']))
    require(ledger['excluded_keys'] == 46 and ledger['natural_bridge_calls'] == 99, '46-key original observations differ')
    concurrent_root = manifest['concurrent'] + '/'
    simultaneous = concurrent(load(concurrent_root + 'result.json'), parse(raw(concurrent_root + 'java-methods.trace')),
                              load(concurrent_root + 'value-summary.json'))
    require(load(concurrent_root + 'helper-identity.json')['sha256'] == manifest['files'][manifest['helper']],
            'concurrent helper identity differs')
    dex = load(manifest['dex_identity'])
    require(dex['apk_sha256'] in original_identity['files'].values() and dex['identical'] is False
            and dex['classes_dex_sha256'] == manifest['files'][manifest['installed_dex']], 'installed DEX identity differs')
    return dict(schema='e2.1b-bounded-evidence.v1', status='bounded_evidence_verified',
                strict_gate='BLOCKED', no_vm_verified=False, android_free_verified=False, training_admissible=False,
                historical_comparison=historical, runs=rows, removability=ledger, concurrent=simultaneous,
                installed_dex_sha256=dex['classes_dex_sha256'], source_apk_sha256=dex['apk_sha256'],
                pn_native_replacement=False, removed_keys=0,
                unresolved=['46-key necessity/removability counterfactuals', 'commit boolean and error contract',
                            'different-value concurrent writes', 'configuration close/free and Java references',
                            'repeated configuration constructor and initialization ABI'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    result = evaluate(manifest, args.manifest.resolve().parent)
    result['manifest_sha256'] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    with args.output.open('x') as target:
        json.dump(result, target, indent=2)
        target.write('\n')
    print(json.dumps(dict(status=result['status'], strict_gate=result['strict_gate'], runs=len(result['runs']),
                          removed_keys=result['removed_keys'], pn_native_replacement=False)))


if __name__ == '__main__':
    main()
