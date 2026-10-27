"""Verify bounded PN concurrency observations and serialized replacement experiments."""

import argparse
import hashlib
import json
from pathlib import Path

from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources

VERIFICATION_SOURCES = {'verify_e2_1n_concurrency.py', 'verify_e0_snapshots.py',
                        'verify_e1_gate.py', 'verify_e1_semantics.py', 'verify_e2_gate.py'}


def lock_metadata(evidence):
    require(evidence['schema'] == 'e2.1n-lock-metadata.v2'
            and evidence['same_store_type'] is True and evidence['distinct_stores'] is True
            and evidence['coverage'] == 'reflected method modifiers only; internal synchronized blocks and lock acquisition unknown',
            'incomplete lock observation scope')
    expected = {
        ('com.supercell.titan.ApplicationUtilBase', 'getKeyValue'): (9, ['java.lang.String'], 'java.lang.String'),
        ('com.supercell.titan.ApplicationUtilBase', 'storeKeyValue'): (9, ['java.lang.String', 'java.lang.String'], 'void'),
        ('com.supercell.titan.GameApp', 'requestBackup'): (1, [], 'void'),
        ('c.a', 'j'): (1, ['java.lang.String'], 'java.lang.String'),
        ('c.a', 'n'): (1, ['java.lang.String', 'java.lang.String'], 'void'),
        ('org.nashura.evidence.E21PreferenceObserver', 'command'): (41, ['java.lang.String'], 'java.lang.String'),
        ('org.nashura.evidence.E21PreferenceObserver', 'nextEditor'): (42, [], 'long'),
        ('org.nashura.evidence.E21PreferenceObserver', 'record'): (42, ['java.lang.String', 'long', 'java.lang.String', 'java.lang.String'], 'void'),
    }
    require(len(evidence['methods']) == len(expected), 'lock method set incomplete')
    seen = set()
    for row in evidence['methods']:
        key = (row['owner'], row['name'])
        require(key in expected and key not in seen, 'unexpected or repeated lock method')
        seen.add(key)
        modifiers, parameters, returns = expected[key]
        require(row['modifiers'] == modifiers and row['parameters'] == parameters and row['returns'] == returns
                and row['synchronized'] is bool(modifiers & 32) and row['static'] is bool(modifiers & 8),
                'lock metadata differs')
    return {'original_method_synchronized_flags': False, 'internal_locking': 'unknown',
            'observer_command_static_synchronized': True}


def race_result(evidence):
    require(evidence['status'] == 0 and evidence['general_thread_safety'] == 'unknown'
            and evidence['no_vm_verified'] is False and len(evidence['workers']) == 2,
            'race completion/scope differs')
    threads = set()
    own = other = 0
    for index, row in enumerate(evidence['workers']):
        require(row['index'] == index and type(row['thread']) is int and row['thread'] > 0
                and row['thread'] not in threads and row['completed'] == 4 and row['error'] == 0
                and row['detached'] is True and row['unknown_reads'] == 0
                and type(row['own_reads']) is int and type(row['other_reads']) is int
                and min(row['own_reads'], row['other_reads']) >= 0
                and row['own_reads'] + row['other_reads'] == 4, 'race worker incomplete')
        threads.add(row['thread'])
        own += row['own_reads']
        other += row['other_reads']
    return threads, {'writes': 8, 'reads': 8, 'own_reads': own, 'other_reads': other,
                     'unknown_reads': 0, 'general_thread_safety': 'unknown'}


def original_timeline(evidence, threads):
    require(evidence['schema'] == 'e2.1n-original-pn-timeline.v4' and evidence['armed'] is True
            and evidence['dropped'] == 0 and evidence['active'] == []
            and evidence['pending_threads'] == 0 and evidence['retained_listeners'] == 2
            and 1 <= evidence['retained_return_sites'] <= 64 and evidence['module_observer_retained'] is True
            and evidence['general_thread_safety'] == 'unknown' and evidence['no_vm_verified'] is False
            and evidence['strict_gate'] == 'BLOCKED'
            and evidence['coverage'] == 'PN native entry and observed caller return instruction only; Java monitors and scheduler ordering unknown',
            'original timeline has gaps or overclaims')
    uncovered = []
    for row in evidence['errors']:
        require(row['error'] == 'Error: PN caller return instruction outside bounded executable coverage'
                and row['operation'] in {'read', 'write'} and type(row['thread']) is int and row['thread'] > 0
                and row['caller'] == 'external-probe' and row['completion'] == 'unknown'
                and row['executable_range'] is None and row['phase']
                and isinstance(row['return_address'], str) and row['return_address'].startswith('0x'),
                'unclassified timeline failure')
        require(row['thread'] not in threads, 'race worker call return coverage incomplete')
        uncovered.append({key: row[key] for key in ['operation', 'phase', 'caller', 'completion']})
    active, entered, workers, overlap_count = {}, set(), [], 0
    for sequence, row in enumerate(evidence['events'], 1):
        require(row['sequence'] == sequence and row['operation'] in {'read', 'write'}
                and type(row['thread']) is int and row['thread'] > 0
                and type(row['wall_milliseconds']) is int, 'timeline event identity differs')
        if row['edge'] == 'enter':
            require(row['call'] == len(entered) + 1 and row['call'] not in entered
                    and row['overlaps'] == list(active), 'timeline overlap attribution differs')
            overlap_count += sum(value['thread'] != row['thread'] for value in active.values())
            entered.add(row['call'])
            active[row['call']] = row
            if row['thread'] in threads:
                require(row['phase'] == 'explicit_post_trajectory_pn_race' and row['caller'] == 'external-probe',
                        'worker entry provenance missing')
                workers.append(row)
        else:
            require(row['edge'] == 'leave' and row['call'] in active
                    and row['completion'] == 'observed caller return instruction', 'unmatched timeline return')
            entry = active.pop(row['call'])
            require(row['thread'] == entry['thread'] and row['operation'] == entry['operation'],
                    'timeline returned on different thread/operation')
    require(not active and evidence['calls'] == len(entered)
            and len(evidence['events']) == len(entered) * 2, 'timeline not fully closed')
    for thread in threads:
        require([row['operation'] for row in workers if row['thread'] == thread] == ['write', 'read'] * 4,
                'worker call sequence differs')
    return {'closed_calls': len(entered), 'cross_thread_entry_overlaps': overlap_count,
            'uncovered_calls': uncovered, 'return_coverage': 'partial' if uncovered else 'complete for observed entries',
            'internal_locking': 'unknown', 'general_thread_safety': 'unknown'}


def backend_prefix(messages):
    events = [row['payload'].get('payload', {}) for row in messages]
    timeline = [row['detail'] for row in events if row.get('event') == 'pn_backend_timeline']
    require(timeline and len(timeline) < 4096, 'backend diagnostic missing or bounded coverage exhausted')
    owner, stages, rejections = None, [], []
    retirement = None
    calls = 0
    for sequence, row in enumerate(timeline, 1):
        require(row['sequence'] == sequence, 'backend diagnostic sequence differs')
        edge = row['edge']
        if row.get('owner') is None and row.get('stage') == 'jni:native-unbind':
            require(owner is None and not stages and calls > 0 and row['thread'] > 0,
                    'backend retirement overlaps a call')
            if edge == 'stage-enter':
                require(retirement is None and sequence == len(timeline) - 1,
                        'backend retirement not terminal')
                retirement = row['thread']
            else:
                require(edge == 'stage-return' and row['thread'] == retirement and sequence == len(timeline),
                        'backend retirement did not return on owning thread')
            continue
        require(retirement is None, 'backend operation after retirement')
        if edge == 'call-enter':
            require(owner is None and row['owner']['call'] == calls + 1
                    and row['owner']['thread'] == row['thread']
                    and row['owner']['operation'] == row['operation'] and row['owner']['store'] == row['store'],
                    'backend call ownership differs')
            owner = dict(row['owner'])
            calls += 1
        elif edge in {'stage-enter', 'stage-return', 'stage-throw'}:
            require(owner is not None and row['thread'] == owner['thread']
                    and row['owner']['call'] == owner['call'] and row['owner']['stage'] == row['stage'],
                    'backend stage ownership differs')
            if edge == 'stage-enter':
                stages.append(row['stage'])
            else:
                require(stages and stages.pop() == row['stage'], 'backend stage return differs')
        elif edge in {'call-return', 'call-throw'}:
            require(owner is not None and not stages and row['owner']['call'] == owner['call']
                    and row['thread'] == owner['thread'], 'backend call return differs')
            owner = None
        else:
            require(edge == 'rejected' and owner is not None and stages
                    and row['busy'] is True and row['retired'] is False
                    and row['reason'] == 'bounded serial PN backend required'
                    and row['owner']['call'] == owner['call'] and row['owner']['thread'] == owner['thread']
                    and row['thread'] != owner['thread'] and row['owner']['stage'] == stages[-1],
                    'backend reentrancy attribution missing')
            rejections.append({'stage': stages[-1], 'operation': row['operation'], 'store': row['store']})
    require(owner is None and not stages, 'backend observed prefix not closed')
    return {'events': len(timeline), 'entered_calls': calls, 'rejections': rejections,
            'observed_prefix': 'closed',
            'bridge_contract_violations': sum(row.get('event') == 'pn_bridge_contract_violation' for row in events),
            'replacement_concurrency_equivalence': 'unverified',
            'native_process_abort_observed': 'unknown'}


def backend_rejection(messages):
    result = backend_prefix(messages)
    require(result['rejections'] and result['bridge_contract_violations'] > 0,
            'backend rejected call/bridge failure evidence incomplete')
    return dict(result, replacement_concurrency_equivalence='failed for observed attempt')


def backend_timeout(messages):
    result = backend_prefix(messages)
    require(not result['rejections'] and result['bridge_contract_violations'] == 0,
            'timeout-only observation contains an explicit rejection or bridge violation')
    return dict(result, termination='external experiment timeout', timeout_cause='unknown',
                race_result='uncovered', saved_store_restore='uncovered after timeout',
                complete_post_race_lifecycle='uncovered after timeout')


def backend_failed_attempt(messages):
    prefix = backend_prefix(messages)
    if not prefix['rejections']:
        return backend_timeout(messages)
    result = backend_rejection(messages)
    return dict(result, termination='external experiment timeout', timeout_cause='unknown',
                race_result='uncovered', saved_store_restore='uncovered after timeout',
                complete_post_race_lifecycle='uncovered after timeout')


def serial_dispatch(bridge, messages):
    gate = bridge['dispatch_gate']
    require(gate['mode'] == 'native-serial-v1'
            and gate['scope'] == 'all native configuration bridge dispatches serialized before JavaScript; general equivalence unknown',
            'serial experiment scope differs')
    counters = gate['counters']
    require(set(counters) == {'entered', 'completed', 'active', 'waiting', 'peak_waiting', 'lock_errors'}
            and all(type(value) is int and value >= 0 for value in counters.values())
            and counters['entered'] == counters['completed'] > 0
            and counters['active'] == counters['waiting'] == counters['lock_errors'] == 0
            and 1 <= counters['peak_waiting'] <= counters['entered'], 'native gate incomplete')
    diagnostic = bridge['dispatch_diagnostics']
    require(diagnostic['mode'] == 'entry-return-v1' and diagnostic['dropped'] == 0
            and diagnostic['calls'] == counters['completed'] == bridge['fallbacks'] + len(bridge['events'])
            and bridge['errors'] == [] and bridge['retired'] is True and bridge['installed'] is False
            and bridge['no_vm_verified'] is False and bridge['strict_gate'] == 'BLOCKED',
            'serial bridge accounting differs')
    streamed = [row['payload'].get('payload', {}).get('detail') for row in messages
                if row['payload'].get('payload', {}).get('event') == 'pn_dispatch_timeline']
    require(streamed == diagnostic['events'], 'dispatch stream/final evidence differs')
    active, stack, entered = None, [], 0
    for sequence, row in enumerate(diagnostic['events'], 1):
        require(row['sequence'] == sequence and row['operation'] in {'read', 'write'}
                and type(row['thread']) is int and row['thread'] > 0 and row['phase']
                and type(row['wall_milliseconds']) is int, 'dispatch provenance missing')
        if row['stage'] == 'dispatch' and row['edge'] == 'enter':
            require(active is None and row['call'] == entered + 1, 'serial dispatch overlapped')
            active, entered = row, entered + 1
        else:
            require(active is not None and row['call'] == active['call']
                    and row['thread'] == active['thread'] and row['operation'] == active['operation'],
                    'dispatch ownership differs')
            if row['stage'] == 'dispatch':
                require(row['edge'] == 'return' and not stack, 'dispatch did not return cleanly')
                active = None
            elif row['edge'] == 'enter':
                stack.append(row['stage'])
            else:
                require(row['edge'] == 'return' and stack and stack.pop() == row['stage'],
                        'dispatch stage failed or did not close')
    require(active is None and not stack and entered == counters['completed'], 'dispatch prefix incomplete')
    native = bridge['native_backend']
    require(native['errors'] == [] and native['retired'] is True and native['ready'] is False
            and native['derivations'] == 1
            and all(row['status'] in {'ready', 'returned'} for row in native['events'])
            and all(row['status'] == 0 and row['receipt']['failure'] == 0 for row in bridge['events']),
            'serial native operation failed')
    result = backend_prefix(messages)
    require(not result['rejections'] and result['bridge_contract_violations'] == 0, 'serial backend rejected a call')
    backend = native['diagnostics']
    streamed_backend = [row['payload'].get('payload', {}).get('detail') for row in messages
                        if row['payload'].get('payload', {}).get('event') == 'pn_backend_timeline']
    require(backend['schema'] == 'e2.1n-backend-timeline.v1' and backend['events'] == streamed_backend
            and backend['dropped'] == 0 and backend['busy'] is False and backend['owner'] is None
            and backend['general_thread_safety'] == 'unknown', 'backend stream/final evidence differs')
    return {'dispatch_calls': entered, 'gate_counters': counters, 'backend': result,
            'scope': gate['scope'], 'general_thread_safety': 'unknown',
            'replacement_concurrency_equivalence': 'unverified outside bounded serialized experiment'}


def cross_run_identities(identities, dispatch_libraries):
    require(len(set(dispatch_libraries)) <= 1, 'multiple dispatch gate paths')
    optional = set(dispatch_libraries)
    require(all(Path(path).name == 'libe21dispatchgate.so' for path in optional),
            'unexpected optional input')
    reference = identities[0]
    gate_hashes = []
    for identity in identities:
        require(identity['update_manifest_sha256'] == reference['update_manifest_sha256'],
                'cross-run update identity differs')
        require({path: digest for path, digest in identity['files'].items() if path not in optional}
                == {path: digest for path, digest in reference['files'].items() if path not in optional},
                'cross-run protected inputs differ')
        for path in optional:
            if path in identity['files']:
                gate_hashes.append(identity['files'][path])
    require(not optional or gate_hashes and len(set(gate_hashes)) == 1,
            'cross-run dispatch gate identity differs')


def evaluate(manifest, base):
    require(manifest['schema'] in {'e2.1n-private-manifest.v1', 'e2.1n-private-manifest.v2'}, 'wrong N manifest')
    base = base.resolve()
    for relative, expected in manifest['files'].items():
        target = base / relative
        require(not Path(relative).is_absolute() and '..' not in Path(relative).parts
                and not any(path.is_symlink() for path in [target, *target.parents]), 'unsafe attachment')
        require(hashlib.sha256(target.read_bytes()).hexdigest() == expected, 'attachment hash differs: ' + relative)

    def raw(relative):
        require(relative in manifest['files'], 'unbound attachment: ' + relative)
        return (base / relative).read_bytes()

    def load(relative):
        return json.loads(raw(relative))

    require(set(manifest['verification_sources']) == VERIFICATION_SOURCES,
            'verification source closure incomplete')
    for name, relative in manifest['verification_sources'].items():
        require(raw(relative) == Path(__file__).with_name(name).read_bytes(), 'verification source differs')
    names = manifest['runs']
    expected = {'control', 'timeline', 'replacement_failure'}
    if manifest['schema'] == 'e2.1n-private-manifest.v2':
        expected |= {'serialized_replacement', 'serialized_replacement_repeat'}
    require(set(names) == expected and len(set(names.values())) == len(expected),
            'independent run set differs')
    identities, process_ids, trajectories, lifecycle_reference = [], set(), [], None
    serialized, post_lifecycles, dispatch_libraries = [], [], []
    for kind, label in names.items():
        require('/' not in label and label not in {'.', '..'}, 'unsafe run label')
        prefix = label + '/'
        attempt, config = load('attempt-' + label + '.json'), load(prefix + 'private-config.json')
        start = load(prefix + 'run-start.json')
        require(attempt['label'] == label and config == load(label + '.config.json') and config['mode'] == 'original'
                and start['config_sha256'] == manifest['files'][label + '.config.json']
                and start['runner_sha256'] == manifest['files'][prefix + 'sources/run_e2_observed.py'],
                'executed configuration/runner differs')
        for name, digest in load(prefix + 'source-identity.json').items():
            require(manifest['files'].get(prefix + 'sources/' + name) == digest, 'runtime source identity differs')
        owned, cleanup = load(prefix + 'owned-process.json'), load(prefix + 'cleanup.json')
        require(owned['pid'] not in process_ids and cleanup['pid'] == owned['pid']
                and cleanup['action'] == 'force-stop completed owned attempt'
                and cleanup['stat'].split(') ', 1)[1].split()[19]
                == owned['stat'].split(') ', 1)[1].split()[19], 'owned process cleanup differs')
        process_ids.add(owned['pid'])
        identity = load(prefix + 'identity-before.json')
        require(identity['files'] == config['guest_identity']
                and identity['update_manifest_sha256'] == config['update_identity_sha256'], 'input identity differs')
        identities.append(identity)
        lock_metadata(load(prefix + 'pn-lock-contract.json'))
        messages = [json.loads(line) for line in raw(prefix + 'messages.jsonl').splitlines()]
        if kind == 'replacement_failure':
            require(attempt['exit_code'] == 124 and prefix + 'run-finished.json' not in manifest['files']
                    and prefix + 'identity-after.json' not in manifest['files']
                    and prefix + 'pn-race.json' not in manifest['files']
                    and prefix + 'pn-race-restore.json' not in manifest['files']
                    and config['pn_bridge']['codec_backend']['diagnostics'] == 'call-timeline-v1',
                    'failed replacement attempt misclassified')
            require(load(prefix + 'pn-race-begin.json') == {'receipt': 'pn-captured\t2'},
                    'replacement race start/save evidence missing')
            rejection = backend_failed_attempt(messages)
        else:
            require(attempt['exit_code'] == 0 and identity == load(prefix + 'identity-after.json'),
                    'successful original run/input identity differs')
            completed_run(base, label, load(prefix + 'run-finished.json'), messages)
            resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
            receipts = load(prefix + 'control-receipts.json')
            main = controls([row for row in receipts if not row['phase'].startswith('post_edge_')])
            if lifecycle_reference is None:
                lifecycle_reference = main
            require(main == lifecycle_reference, 'native lifecycle differs')
            if kind.startswith('serialized_replacement'):
                require(config['pn_bridge']['dispatch_gate']['mode'] == 'native-serial-v1'
                        and config['pn_bridge']['dispatch_diagnostics'] == 'entry-return-v1'
                        and config['pn_bridge']['codec_backend']['diagnostics'] == 'call-timeline-v1',
                        'serialized replacement configuration differs')
                gate_library = config['pn_bridge']['dispatch_gate']['library']
                require(gate_library in identity['files'], 'dispatch gate input identity missing')
                dispatch_libraries.append(gate_library)
                threads, serial_race = race_result(load(prefix + 'pn-race.json'))
                result = serial_dispatch(load(prefix + 'pn-bridge-final.json'), messages)
                observed = load(prefix + 'pn-bridge-final.json')['dispatch_diagnostics']['events']
                for thread in threads:
                    require([row['operation'] for row in observed if row['thread'] == thread
                             and row['stage'] == 'dispatch' and row['edge'] == 'enter'] == ['write', 'read'] * 4,
                            'serialized worker sequence differs')
                serialized.append(dict(result, run=label, race=serial_race))
            else:
                require('pn_bridge' not in config, 'original run contains replacement')
        pairs = [('snapshots', 'trace.json')]
        if kind == 'timeline' or kind.startswith('serialized_replacement'):
            threads, race = race_result(load(prefix + 'pn-race.json'))
            if kind == 'timeline':
                original_race = race
                timeline = original_timeline(load(prefix + 'pn-timeline-final.json'), threads)
            require(load(prefix + 'pn-race-begin.json') == {'receipt': 'pn-captured\t2'}
                    and load(prefix + 'pn-race-restore.json') == {'receipt': 'pn-restored\t2'},
                    'race state restore differs')
            post = []
            for row in receipts:
                if row['phase'].startswith('post_edge_'):
                    post.append(dict(row, phase=row['phase'].removeprefix('post_edge_')))
            post_lifecycles.append(controls(post))
            pairs.append(('post-edge-snapshots', 'post-edge-trace.json'))
        for directory, receipt in pairs:
            for path in (base / label / directory).iterdir():
                raw(str(path.relative_to(base)))
            result = verify(base / names['control'] / 'snapshots', base / label / directory,
                            load(names['control'] + '/trace.json'), load(prefix + receipt))
            require(all(result[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'native trajectory differs')
            trajectories.append(result)
    cross_run_identities(identities, dispatch_libraries)
    require(all(rows == post_lifecycles[0] for rows in post_lifecycles),
            'original/replacement post-race lifecycle differs')
    return {'schema': 'e2.1n-bounded-diagnostics.v1', 'status': 'bounded_diagnostics_verified',
            'complete_trajectories': len(trajectories), 'snapshots_per_trajectory': 3602,
            'trajectory_sha256': EXPECTED_TRAJECTORY['trajectory_sha256'], 'race': original_race,
            'original_timeline': timeline, 'replacement_failure': rejection,
            'replacement_failure_final_identity': 'uncovered after timeout',
            'serialized_replacement_experiments': serialized,
            'strict_gate': 'BLOCKED', 'removed_keys': 0, 'no_vm_verified': False,
            'android_free_verified': False, 'training_admissible': False,
            'general_thread_safety': 'unknown', 'attachment_count': len(manifest['files'])}


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
    print(json.dumps({key: result[key] for key in ['status', 'strict_gate', 'attachment_count']}))


if __name__ == '__main__':
    main()
