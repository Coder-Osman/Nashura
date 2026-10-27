"""Check identity-bound native E2.1 evidence without promoting unresolved semantics."""

import argparse
import hashlib
import json
from pathlib import Path

from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources


def bridge_semantics(data):
    rows = data['results']
    require(len({row['case'] for row in rows}) == len(rows), 'duplicate semantic case')
    cases = {row['case']: row for row in rows}
    for row in rows:
        state = row.get('exception_state')
        if isinstance(state, dict):
            require(state['before'] is False and state['after'] is False, 'pending probe exception')
        if row['operation'] == 'read':
            result = row['result']
            raw = result['value'].encode('utf-8')
            require(len(raw) == result['length'] and list(raw) == result['bytes'], 'semantic byte/length mismatch')
    stable = [cases[name]['result']['value'] for name in
              ['after_cold_ready', 'after_reset', 'after_terminal_reset']]
    require(len(set(stable)) == 1, 'reset changed observed PN value')
    if 'empty_write' in cases:
        for name in ['empty', 'malformed', 'utf8', 'shortest', 'boundary_4095']:
            require(cases[name + '_write']['input'] == cases[name + '_read']['result']['value'], 'bridge roundtrip differs')
        require(cases['empty_read']['result']['length'] == 0
                and cases['boundary_4095_read']['result']['length'] == 4095, 'bridge boundary missing')
        require(cases['normal_repeat']['result']['value'] == cases['restored_read']['result']['value'] == stable[0],
                'probe restoration differs')
    return dict(cases=len(rows), reset_observation='stable', pn_native_replacement=False)


def string_lifecycle(data):
    expected_lengths = dict(empty=0, inline_one=1, inline_seven=7, heap_eight=8,
                            utf8_inline=7, utf8_heap=10, heap_4095=4095)
    require(len(data['rows']) == 7 and {row['case'] for row in data['rows']} == expected_lengths.keys(),
            'string lifecycle cases missing')
    for row in data['rows']:
        require(row['length'] == expected_lengths[row['case']], 'string lifecycle length differs')
        expected = int(row['length'] >= 8)
        require(row['heap'] == bool(expected) and row['frees_observed'] == row['expected_frees'] == expected
                and row['status'] == 'verified', 'string destruction differs')
        require(sum(event['case'] == row['case'] and event['event'] == 'libc.free'
                    for event in data['events']) == expected, 'string free event missing')


def pn_cold_chain(series):
    require(len(series) == 4, 'cold chain incomplete')
    probes = [{row['case']: row for row in run['probe']['results']} for run in series]
    wrapper = probes[0]
    require(wrapper['missing_exists']['result']['boolean'] is False
            and wrapper['top_exists']['result']['boolean'] is True
            and wrapper['removed_exists']['result']['boolean'] is False, 'exists/remove differs')
    require(wrapper['top_write']['input'] == wrapper['top_read']['result']['value']
            == wrapper['bridge_read']['result']['value'], 'top/raw read differs')
    require(wrapper['removed_read']['result']['length'] == wrapper['removed_bridge_read']['result']['length'] == 0,
            'deleted key still readable')
    require(wrapper['prepare_empty']['input'] == '', 'empty preparation differs')
    require(probes[2]['missing_verified']['result']['boolean'] is False
            and probes[2]['missing_bridge_read']['result']['length'] == 0, 'missing preparation differs')
    expected_inputs = [wrapper['prepare_empty']['input'], probes[1]['prepare_malformed']['input'], '']
    require(expected_inputs[1] and not expected_inputs[1].isdecimal(), 'malformed preparation differs')
    for index, run in enumerate(series[1:]):
        natural = [row for row in run['observer']['config_calls']
                   if row['key'].get('value') == 'PN_POPUP'
                   and row['operation'] in {'PreferenceNativeReadBridge', 'PreferenceNativeWriteBridge'}]
        require(len(natural) >= 2 and natural[0]['operation'] == 'PreferenceNativeReadBridge'
                and natural[1]['operation'] == 'PreferenceNativeWriteBridge', 'natural cold read/write missing')
        read, write = natural[:2]
        require(read['returned_string']['status'] == write['value']['status'] == 'decoded'
                and read['returned_string']['value'] == expected_inputs[index], 'cross-process input differs')
        require(read['sequence'] < write['sequence'] and read['thread'] == write['thread'], 'cold order/thread differs')
        require(write['value']['value'] == '1'
                == probes[index + 1]['after_cold_ready']['result']['value'], 'natural cold writeback differs')
    require(probes[3]['restore_saved']['input'] == probes[3]['restored_bridge_read']['result']['value'],
            'saved PN restoration differs')
    return dict(original_cold_transitions=3, top_raw_exists_remove='verified',
                native_persistence_verified=False, commit_timing='unknown', repeated_constructor='not tested')


def replacement_events(receipt, config, probe=None):
    status = receipt['status']
    entries = config['replacement']['entries']
    excluded = config['replacement']['excluded_keys']
    require(not entries.keys() & excluded.keys(), 'overlapping routes')
    require(receipt['mode'] == config['mode'] and receipt['errors'] == [], 'replacement mode or error differs')
    require(status['installed'] and status['mismatches'] == status['targeted_fallback'] == 0,
            'replacement mismatch or targeted fallback')
    require(receipt['store']['failures'] == '0', 'store failure')
    events = receipt['events']
    require(all(not event.get('error') and event['targeted'] == (event['key'] in entries)
                for event in events), 'event error or target misattribution')
    reads = [event for event in events if event.get('targeted') and event['operation'] == 'read']
    writes = [event for event in events if event.get('targeted') and event['operation'] == 'write']
    require(len(reads) == status['targeted_reads'] == 32 and len(writes) == status['targeted_writes'] == 4,
            'targeted calls missing')
    require({event['key'] for event in reads} == entries.keys(), 'declared reads missing')
    require({event['key'] for event in writes} == {key for key, entry in entries.items() if entry['writable']},
            'declared writes missing')
    require(receipt['store']['reads'] == str(len(reads)) and receipt['store']['writes'] == str(len(writes)),
            'store accounting differs')
    for event in reads:
        require(event['expected'] == event['actual'] == entries[event['key']]['value'], 'wrong replacement value')
        require(event['route'] == ('shadow_read_original' if config['mode'] == 'shadow' else 'native_read'), 'wrong read route')
    for event in writes:
        require(event['value'] == entries[event['key']]['value'], 'wrong write value')
        require(event['route'] == ('shadow_write_original' if config['mode'] == 'shadow' else 'native_write'), 'wrong write route')
    fallback = [event for event in events if event['route'] == 'original_fallback']
    undeclared = [event for event in fallback if event['key'] not in excluded]
    if undeclared:
        declared = [case for case in config.get('semantics_cases', [])
                    if case['case'] == 'missing_read' and case['operation'] == 'read']
        observed = [case for case in (probe or {}).get('results', []) if case['case'] == 'missing_read']
        require(config['mode'] != 'strict-targeted' and len(undeclared) == len(declared) == len(observed) == 1,
                'unexplained fallback')
        event, case, result = undeclared[0], declared[0], observed[0]
        require(event['operation'] == 'read' and event['key'] == case['key'] == result['key']
                and event['thread'] == result['thread'] and event['native_scope'] is None
                and result['result']['length'] == 0, 'unexplained probe fallback')
    return dict(fallback_calls_including_probes=len(fallback), explicit_missing_probe_fallbacks=len(undeclared),
                targeted_reads=len(reads), targeted_writes=len(writes))


def evaluate(manifest, base):
    require(manifest.get('schema') == 'e2.1-private-manifest.v1', 'wrong manifest schema')
    verified = {}
    for relative, expected in manifest['files'].items():
        path = Path(relative)
        require(not path.is_absolute() and '..' not in path.parts, 'unsafe attachment path')
        target = base / path
        require(not any(parent.is_symlink() for parent in [target, *target.parents]), 'symlink attachment')
        require(hashlib.sha256(target.read_bytes()).hexdigest() == expected, 'attachment hash mismatch: ' + relative)
        verified[relative] = expected

    def load(relative):
        require(relative in verified, 'unbound attachment: ' + relative)
        return json.loads((base / relative).read_text())

    ledger = load(manifest['ledger'])
    require(ledger['unique_excluded_keys'] == len(ledger['rows']) == 46, '46-key ledger missing')
    require(len({row['key'] for row in ledger['rows']}) == 46, 'duplicate ledger key')
    require(ledger['fallback_events_count'] == len(ledger['fallback_events']) == 99, 'historical fallback ledger differs')
    require(all(row['replacement_class'] in {'required', 'removable_after_proof', 'unknown'}
                for row in ledger['rows']), 'unrecognized exclusion class')
    baseline = manifest['original']
    require(baseline in manifest['runs'] and len(set(manifest['runs'])) == len(manifest['runs']),
            'baseline or unique runs missing')
    require(load(baseline + '/private-config.json')['mode'] == 'original', 'baseline is not original')
    original_trace = load(baseline + '/trace.json')
    original_controls = controls(load(baseline + '/control-receipts.json'))
    original_identity = load(baseline + '/identity-before.json')
    processes = set()
    runs = []
    covered = None
    modes = []
    for run in manifest['runs']:
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        finished = load(prefix + 'run-finished.json')
        message_path = prefix + 'messages.jsonl'
        require(message_path in verified, 'unbound messages')
        messages = [json.loads(line) for line in (base / message_path).read_text().splitlines() if line.strip()]
        completed_run(base, run, finished, messages)
        owned = load(prefix + 'owned-process.json')
        identity = load(prefix + 'identity-before.json')
        require(identity == load(prefix + 'identity-after.json') == original_identity, 'input identity changed')
        require(identity == manifest['required_identity'], 'frozen input identity differs')
        require(identity['files'] == config['guest_identity']
                and identity['update_manifest_sha256'] == config['update_identity_sha256'], 'config input identity differs')
        require(owned['pid'] not in processes, 'process evidence reused')
        processes.add(owned['pid'])
        start = load(prefix + 'run-start.json')
        source_config = manifest['source_configs'][run]
        require(verified[source_config] == start['config_sha256'] and load(source_config) == config,
                'run config identity mismatch')
        require(verified[prefix + 'sources/run_e2_observed.py'] == start['runner_sha256'], 'runner source identity mismatch')
        for name, digest in load(prefix + 'source-identity.json').items():
            require(verified.get(prefix + 'sources/' + name) == digest, 'observer source identity mismatch')
        for tick in range(3602):
            require(prefix + 'snapshots/tick-' + str(tick).zfill(6) + '.bin' in verified, 'unbound snapshot')
        comparison = verify(base / baseline / 'snapshots', base / run / 'snapshots',
                            original_trace, load(prefix + 'trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'trajectory differs')
        require(controls(load(prefix + 'control-receipts.json')) == original_controls, 'native lifecycle differs')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        observer = load(prefix + 'e2-final.json')
        require(observer['errors'] == [] and observer['dropped']['errors'] == 0
                and observer['dropped']['config_calls'] == 0, 'observer errors or dropped configuration evidence')
        require(set(observer['dropped']) == {'errors', 'config_calls', 'jni_samples'}, 'unknown dropped evidence category')
        row = dict(run=run, mode=config['mode'], comparison=comparison,
                   jni_samples_beyond_per_bucket_limit=observer['dropped']['jni_samples'],
                   exhaustive_jni_arguments_verified=False)
        probe = load(prefix + 'semantics-final.json') if config.get('semantics_probe') else None
        if probe:
            row['bridge_semantics'] = bridge_semantics(probe)
        if 'replacement' in config:
            receipt = load(prefix + 'replacement-final.json')
            entries = config['replacement']['entries']
            row.update(replacement_events(receipt, config, probe))
            declared = set(entries)
            covered = declared if covered is None else covered & declared
        modes.append(config['mode'])
        runs.append(row)
    require({'original', 'shadow', 'replacement', 'strict-targeted'} <= set(modes), 'required modes missing')
    string_lifecycle(load(manifest['string_lifecycle']))
    require(len(set(manifest['cold_start_runs'])) == 4
            and set(manifest['cold_start_runs']) <= set(manifest['runs']), 'cold chain runs missing')
    require(all(load(run + '/private-config.json')['mode'] == 'original' for run in manifest['cold_start_runs']),
            'cold chain is not original')
    cold_chain = pn_cold_chain([dict(probe=load(run + '/semantics-final.json'),
                                   observer=load(run + '/e2-final.json')) for run in manifest['cold_start_runs']])
    negative = load(manifest['unknown_key_negative'])
    require(negative['status'] == 'failed' and negative['exit_code'] != 0, 'unknown key negative accepted')
    violation_path = manifest['unknown_key_messages']
    require(violation_path in verified, 'unbound negative messages')
    violations = [json.loads(line) for line in (base / violation_path).read_text().splitlines() if line.strip()]
    require(any(row.get('payload', {}).get('payload', {}).get('event') == 'e2_contract_violation'
                for row in violations), 'native contract violation not observed')
    unresolved = [row['key'] for row in ledger['rows']
                  if row['replacement_class'] == 'required' and row['key'] not in (covered or set())]
    return dict(schema='e2.1-gate.v1', status='blocked', exit_code=2,
                verifier_scope='bounded 32-key evidence and probe checks; unresolved semantics fail closed',
                bounded_native_comparisons='passed', runs=runs,
                required_unreplaced_count=len(unresolved), required_unreplaced=unresolved,
                reasons=['required excluded keys still use original JNI fallback',
                         'PN_POPUP native persistence and commit semantics not implemented',
                         'game error/exception, concurrent access and Java reference retirement unclosed',
                         'initialization object sizes, ownership and failure cleanup incomplete'],
                string_component_lifecycle='verified for seven owned native strings only',
                pn_original_cold_chain=cold_chain,
                unknown_key_negative='verified', attachment_count=len(verified),
                attachment_sha256=verified, no_vm_verified=False, android_free_verified=False,
                training_admissible=False, tenfold_speedup_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(args.manifest.read_text()), args.manifest.parent)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as failure:
        result = dict(schema='e2.1-gate.v1', status='blocked', exit_code=2, evidence_validation='failed',
                      reasons=[str(failure)], no_vm_verified=False, android_free_verified=False, training_admissible=False)
    with args.output.open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'attachment_sha256'}))
    raise SystemExit(result['exit_code'])


if __name__ == '__main__':
    main()
