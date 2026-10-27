"""Verify bounded native PN edge replacement and complete post-edge trajectories."""

import argparse
import hashlib
import json
from pathlib import Path

from e2_1_commit_events import commit_counts, delta, parse_events
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources
from verify_e2_1g_pn_codec import xml_entries
from verify_e2_1i_cold_pn_bridge import natural_values, pn_rows
from verify_e2_1k_natural_codec import ROUTE, SCOPE
from verify_e2_1l_edges import ORDERS, validate_matrix


SUFFIXES = ['edges', 'malformed-first', 'padding-first', 'short-state', 'padding-state']
UNSUPPORTED = ['unobserved-base64', 'binary-or-invalid-utf8', 'commit-false-equivalence', 'concurrency']
BUILD_SOURCES = {'E21PreferenceObserver.java', *[
    'e2_1_pn_' + name + suffix for name in ['bridge', 'derive', 'codec', 'stream']
    for suffix in ['.c', '.h']]}
SOURCES = ['verify_e2_1m_persistent_bridge.py', 'verify_e2_1l_edges.py',
    'verify_e2_1k_natural_codec.py', 'verify_e2_1g_pn_codec.py', 'verify_e0_snapshots.py',
    'verify_e2_1i_cold_pn_bridge.py', 'verify_e2_1h_pn_bridge.py', 'verify_e2_1d_evidence.py',
    'e2_1k_path_trace.py', 'e2_1_partial_method_trace.py', 'e2_1_method_trace.py',
    'verify_e1_gate.py', 'verify_e2_gate.py', 'e2_1_commit_events.py',
    'e2_1_pn_edge_matrix.py', 'e2_1_pn_native_backend.js', 'e2_1_pn_bridge.js',
    'e2_1_pn_bridge.c', 'e2_1_pn_bridge.h', 'e2_1_pn_stream.c', 'e2_1_pn_stream.h',
    'e2_1_pn_codec.c', 'e2_1_pn_codec.h', 'e2_1_pn_derive.c', 'e2_1_pn_derive.h',
    'E21PreferenceObserver.java', 'e2_1_return_observer.js', 'run_e2_observed.py',
    'e2_1_config_probe.js', 'e0_dependency_observer.js', 'e2_jni_config_observer.js',
    'e1_resource_redirect.js', 'e1_fmod_binding.js']


def lifecycle(rows, post):
    selected = []
    for row in rows:
        if row['phase'].startswith('post_edge_') is not post:
            continue
        copy = dict(row)
        if post:
            copy['phase'] = copy['phase'].removeprefix('post_edge_')
        selected.append(copy)
    return controls(selected)


def validate_pair(original, replacement, suffix, original_config, replacement_config):
    reference = validate_matrix(original, 'original-' + suffix, original_config)
    require(replacement['schema'] == original['schema'] and replacement['scope'] == original['scope']
            and replacement['replacement'] is True and replacement['restored'] is True
            and replacement['restore_receipt'] == 'pn-restored\t2'
            and replacement['strict_gate'] == 'BLOCKED' and replacement['no_vm_verified'] is False
            and replacement['concurrency_equivalence'] == 'unknown', 'replacement edge scope differs')
    names = ORDERS['original-' + suffix]
    require(replacement['case_order'] == names
            and [row['case'] for row in replacement['cases']] == names, 'replacement edge order differs')
    before = [xml_entries(row) for row in replacement['xml_before']]
    restored = [xml_entries(row) for row in replacement['xml_restored']]
    require(len(before) == 2 and before == restored, 'replacement XML semantic restore differs')
    changed = [set(), set()]
    parsed = []
    for row in replacement['cases']:
        xml = [xml_entries(item) for item in row['xml_after']]
        require(len(xml) == 2, 'replacement XML stores missing')
        parsed.append(xml)
        for store in range(2):
            changed[store].update(key for key in before[store].keys() | xml[store].keys()
                if key != 'PN_POPUP' and before[store].get(key) != xml[store].get(key))
    require(all(len(keys) == 1 for keys in changed), 'unique replacement encrypted PN key missing')
    keys = [next(iter(items)) for items in changed]
    prior_events, prior_bridge = None, None
    for first, second, xml in zip(original['cases'], replacement['cases'], parsed):
        for field in ['case', 'kinds', 'operation', 'fixtures', 'raw_after']:
            require(first[field] == second[field], 'edge differential differs: ' + field)
        native = dict(second['native'])
        require(type(native['thread']) is int and native['thread'] > 0, 'replacement native thread missing')
        native.pop('thread')
        expected = dict(first['native'])
        expected.pop('thread')
        require(native == expected and native['entry_offset'] == replacement_config[
                second['operation'] + '_bridge'], 'native edge result/provenance differs')
        before_bridge, after_bridge = second['bridge_before'], second['bridge_after']
        require(before_bridge['errors'] == after_bridge['errors'] == []
                and before_bridge['native_backend']['errors'] == after_bridge['native_backend']['errors'] == []
                and (prior_bridge is None or before_bridge == prior_bridge), 'edge bridge sequence/error differs')
        prefix = before_bridge['events']
        require(after_bridge['events'][:-1] == prefix, 'edge did not add exactly one bridge call')
        event = after_bridge['events'][-1]
        require(event['operation'] == second['operation'] and event['caller'] == 'external-probe'
                and event['phase'] == 'explicit_post_trajectory_pn_edge_matrix'
                and event['thread'] == second['native']['thread'] and event['status'] == 0
                and event['receipt']['failure'] == 0 and event['route'] == ROUTE,
                'edge replacement invocation/provenance differs')
        if second['operation'] == 'read':
            require(event['result'] == second['native']['result']['value'], 'edge replacement read differs')
        else:
            require(event['input'] == second['native']['input'], 'edge replacement write differs')
        backend_prefix = before_bridge['native_backend']['events']
        backend_events = after_bridge['native_backend']['events']
        require(backend_events[:len(backend_prefix)] == backend_prefix
                and len(backend_events) > len(backend_prefix), 'edge native backend not called')
        delegated_calls = backend_events[len(backend_prefix):]
        require(all(row['status'] == 'returned' and row['thread'] == event['thread']
                    for row in delegated_calls)
                and event['receipt']['reads'] == sum(row['operation'] == 'read' for row in delegated_calls)
                and event['receipt']['writes'] == sum(row['operation'] == 'write' for row in delegated_calls),
                'edge backend/bridge operation counts differ')
        prior_bridge = after_bridge
        events = delta(second['events_before'], second['events_after'])
        original_events = delta(first['events_before'], first['events_after'])
        require(len({row['thread'] for row in events}) == 1
                and commit_counts(events) == commit_counts(original_events), 'edge delegated commit differs')
        current = parse_events(second['events_before'])
        require(prior_events is None or current[:len(prior_events)] == prior_events,
                'replacement observer history changed')
        prior_events = parse_events(second['events_after'])
        for store in range(2):
            expected_xml = dict(before[store])
            for key, value in [('PN_POPUP', second['fixtures'][store]['plain']),
                               (keys[store], second['raw_after'][store].get('encoded'))]:
                if value is None:
                    expected_xml.pop(key, None)
                else:
                    expected_xml[key] = ('string', (('name', key),), value, ())
            require(xml[store] == expected_xml, 'replacement XML differs or unrelated preference changed')
    return reference


def validate_bridge(cold, final, installed, bridge, uninstall, release, observed, case_count):
    require(cold['references'] == dict(allocated_globals=1, deleted_globals=0, retired=False)
            and final['references'] == dict(allocated_globals=1, deleted_globals=1, retired=True)
            and uninstall == {'result': 'uninstalled\t2'} and release == {'result': 'released\t6'},
            'original observer references not released')
    for observer in [cold, final]:
        state = observer['cold']
        require(state['armed'] is True and state['attempted'] is True and state['installed'] is True
                and state['installing'] is False and state['reentrant_entries'] == 0
                and state['errors'] == [] and state['install_result'] == 'installed\t2', 'natural installation failed')
        entry = state['install_entry']
        require(entry['entry'] == 1 and entry['operation'] == 'read' and entry['offset'] == 21022348
                and type(entry['thread']) is int and entry['thread'] > 0, 'natural installation provenance missing')
    require(cold['cold']['windows'] == final['cold']['windows']
            and len(final['cold']['windows']) == 2 and cold['cold']['external_windows'] == 1
            and final['cold']['external_windows'] == 3 + case_count, 'natural window coverage differs')
    for state, retired in [(installed, False), (bridge, True)]:
        require(state['schema'] == 'e2.1m-cold-pn-persistent-bridge.v1' and state['scope'] == SCOPE
                and state['installed'] is not retired and state['retired'] is retired
                and state['errors'] == [] and state['strict_gate'] == 'BLOCKED'
                and state['retained_allocations'] == 10
                and type(state['fallbacks']) is int and state['fallbacks'] >= 0
                and state['no_vm_verified'] is False and state['original_xml_writer_replaced'] is False
                and state['backup_delivery'] == state['concurrency_equivalence'] == 'unknown', 'bridge lifecycle differs')
        native = state['native_backend']
        require(native['schema'] == 'e2.1m-natural-pn-persistent-codec.v1'
                and native['mode'] == 'persistent-pn-edges-v1'
                and native['started'] is True and native['ready'] is not retired
                and native['retired'] is retired and native['derivations'] == 1 and native['errors'] == []
                and native['unsupported'] == UNSUPPORTED and native['strict_gate'] == 'BLOCKED'
                and native['no_vm_verified'] is False and native['xml_backend'] == 'original SharedPreferences'
                and native['original_store_constructors'] == 'retained', 'native backend lifecycle differs')
        for row in native['events']:
            require(row['status'] in {'ready', 'returned'} and type(row['thread']) is int and row['thread'] > 0,
                    'native backend operation failed/unattributed')
        for row in state['events']:
            require(row['status'] == 0 and row['receipt']['failure'] == 0 and row['route'] == ROUTE
                    and type(row['thread']) is int and row['thread'] > 0, 'bridge event failed/unattributed')
    require(len(installed['events']) == 3 and len(bridge['events']) == 5 + case_count
            and bridge['events'][:len(installed['events'])] == installed['events'], 'bridge prefix/count changed')
    require(bridge['native_backend']['events'][:len(installed['native_backend']['events'])]
            == installed['native_backend']['events'], 'backend prefix changed')
    before_value, after_value = natural_values(observed)
    require(observed['errors'] == [] and observed['dropped']['errors'] == 0
            and observed['dropped']['config_calls'] == 0, 'config observer lost relevant evidence')
    for index, operation, caller in [(0, 'read', '0x1394198'), (1, 'write', '0x13942f0')]:
        event, window = bridge['events'][index], final['cold']['windows'][index]
        require(event['operation'] == window['operation'] == operation and event['caller'] == 'libg.so+' + caller
                and window['caller'] == caller and window['bridge_event'] == index
                and event['thread'] == window['thread'] and window['completion'] == 'original callsite return instruction'
                and window['module_base'] == event['module_base'] and window['return_site'] == event['return_site']
                and int(event['module_base'], 16) > 0 and int(event['module_base'], 16) % 4096 == 0
                and int(event['return_site'], 16) == int(event['module_base'], 16) + int(caller, 16),
                'natural callsite/return attribution differs')
        delegated = delta(window['before'], window['after'])
        operations = [('p', name) for name in ['contains', 'contains', 'getString']] if not index else [
            (store, name) for store in ['o', 'p']
            for name in ['contains', 'contains', 'getString', 'edit', 'putString', 'commit']]
        require([(row['store'], row['operation']) for row in delegated]
                == [(store, name + ':' + edge) for store, name in operations for edge in ['enter', 'exit']]
                and len({row['thread'] for row in delegated}) == 1
                and commit_counts(delegated) == ({} if not index else {'o:true': 1, 'p:true': 1}),
                'natural raw operation order/commit differs')
        rows = pn_rows(observed, 'PreferenceRead' if not index else 'PreferenceWrite')
        require(len(rows) == 1 and rows[0]['thread'] == event['thread']
                and rows[0]['phase'] == ('configuration' if not index else 'cold_ready_wait')
                and rows[0]['caller'] == ('libg.so+0x76cae0' if not index else 'libg.so+0x76cfcc'),
                'independent upper natural attribution differs')
        receipt = dict(reads=1, writes=0, changed=0, backup_method_returns=0, failure=0) if not index else dict(
            reads=2, writes=2, changed=3, backup_method_returns=1, failure=0)
        require(event['receipt'] == receipt and (event['result'] == before_value if not index
                else event['input'] == after_value), 'natural return/receipt differs')
    require(final['cold']['windows'][0]['entry'] < final['cold']['windows'][1]['entry'], 'natural order differs')
    for event, phase in zip(bridge['events'][2:5], ['cold_ready_wait', 'after_reset', 'final_reset_state']):
        require(event['operation'] == 'read' and event['caller'] == 'external-probe' and event['phase'] == phase
                and event['result'] == after_value and event['receipt'] == dict(reads=1, writes=0, changed=0,
                    backup_method_returns=0, failure=0), 'explicit intact-state read differs')
    require(final['cold']['entries'] == bridge['fallbacks'] + len(bridge['events']), 'bridge entries not accounted')
    return dict(natural_windows=2, decoder_calls=bridge['native_backend']['decoder_calls'],
                decoder_failures=bridge['native_backend']['decoder_failures'],
                non_pn_original_bridge_fallbacks=bridge['fallbacks'], concurrency_equivalence='unknown')


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1m-private-manifest.v1', 'wrong M manifest')
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

    for name in SOURCES:
        require(raw(manifest['verification_sources'][name]) == Path(__file__).with_name(name).read_bytes(),
                'verification source differs: ' + name)
    reproduction = load('build-reproduction.json')
    require(reproduction['status'] == 'matched' and len(reproduction['commands']) == 3
            and all(row['exit_code'] == 0 for row in reproduction['commands']), 'artifact reproduction failed')
    require(set(reproduction['artifacts']) == {'library', 'dex'}, 'compiled artifact set differs')
    require(set(reproduction['sources']) == BUILD_SOURCES, 'compiled source set differs')
    for name, digest in reproduction['sources'].items():
        require(digest == manifest['files'][manifest['verification_sources'][name]], 'compiled source differs')
    for item in reproduction['artifacts'].values():
        require(hashlib.sha256(raw(item['original'])).hexdigest()
                == hashlib.sha256(raw(item['rebuilt'])).hexdigest() == item['sha256'], 'compiled artifact differs')
    historical = load('historical-identity.json')
    artifact_guests = {row['guest'] for row in reproduction['artifacts'].values()}
    immutable = {path: digest for path, digest in historical['files'].items() if path not in artifact_guests
                 and not path.endswith(('libe21l_bridge.so', 'e21l-observer.dex'))}
    require(len(immutable) == 7, 'historical immutable input set differs')
    pairs = manifest['pairs']
    require([row['suffix'] for row in pairs] == SUFFIXES, 'independent pair set differs')
    results, processes, reference_controls = [], set(), None
    reference = pairs[0]['original']
    for pair in pairs:
        for side in ['original', 'replacement']:
            label = pair[side]
            require('/' not in label and label not in {'.', '..'}, 'unsafe run label')
            prefix = label + '/'
            attempt = load('attempt-' + label + '.json')
            require(attempt['label'] == label and attempt['exit_code'] == 0
                    and attempt['seconds'] > 0, 'successful owned attempt missing')
            config = load(prefix + 'private-config.json')
            require(config == load(label + '.config.json') and config['mode'] == 'original'
                    and config['pn_edge_matrix'] is True and config['pn_post_edge_lifecycle'] is True
                    and not any(name in config for name in ['replacement', 'pn_stream', 'pn_direct_cipher']),
                    'executed scope/configuration differs')
            start = load(prefix + 'run-start.json')
            require(start['config_sha256'] == manifest['files'][label + '.config.json']
                    and start['runner_sha256'] == manifest['files'][prefix + 'sources/run_e2_observed.py'],
                    'executed config/runner identity differs')
            for name, digest in load(prefix + 'source-identity.json').items():
                require(manifest['files'].get(prefix + 'sources/' + name) == digest, 'archived runtime source differs')
            for name in SOURCES:
                archived = prefix + 'sources/' + name
                if archived in manifest['files']:
                    require(raw(archived) == raw(manifest['verification_sources'][name]), 'executed source differs: ' + name)
            completed_run(base, label, load(prefix + 'run-finished.json'),
                [json.loads(row) for row in raw(prefix + 'messages.jsonl').splitlines()])
            owned, cleanup = load(prefix + 'owned-process.json'), load(prefix + 'cleanup.json')
            require(owned['pid'] not in processes and cleanup['pid'] == owned['pid']
                    and cleanup['action'] == 'force-stop completed owned attempt'
                    and cleanup['stat'].split(') ', 1)[1].split()[19]
                    == owned['stat'].split(') ', 1)[1].split()[19], 'independent owned process cleanup differs')
            processes.add(owned['pid'])
            identity = load(prefix + 'identity-before.json')
            require(identity == load(prefix + 'identity-after.json') and len(identity['files']) == 9
                    and identity['files'] == config['guest_identity']
                    and identity['update_manifest_sha256'] == historical['update_manifest_sha256']
                    == config['update_identity_sha256']
                    and all(identity['files'].get(path) == digest for path, digest in immutable.items()),
                    'frozen input identity differs')
            for item in reproduction['artifacts'].values():
                require(identity['files'][item['guest']] == item['sha256'], 'executed compiled artifact differs')
            updates = [row for row in map(json.loads, raw(prefix + 'commands.jsonl').splitlines())
                       if row['argv'][-1].startswith('find ') and 'update -type f -exec sha256sum' in row['argv'][-1]]
            require(len(updates) == 2 and all(row['code'] == 0 and len(row['stdout'].splitlines()) == 990
                    and hashlib.sha256(row['stdout'].strip().encode()).hexdigest() == identity['update_manifest_sha256']
                    for row in updates), '990 update file before/after identity missing')
            receipts = load(prefix + 'control-receipts.json')
            ordinary, post = lifecycle(receipts, False), lifecycle(receipts, True)
            if reference_controls is None:
                reference_controls = (ordinary, post)
            require((ordinary, post) == reference_controls, 'native lifecycle/action/snapshot/restore/reset differs')
            for directory, receipt in [('snapshots', 'trace.json'), ('post-edge-snapshots', 'post-edge-trace.json')]:
                for path in (base / label / directory).iterdir():
                    raw(str(path.relative_to(base)))
                result = verify(base / reference / 'snapshots', base / label / directory,
                    load(reference + '/trace.json'), load(prefix + receipt))
                require(all(result[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'complete trajectory differs')
            resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
            if side == 'original':
                require('pn_bridge' not in config, 'original reference replaced')
            else:
                bridge_config = config['pn_bridge']
                require(bridge_config['mode'] == 'replacement' and bridge_config['install_at'] == 'first-native-bridge'
                        and bridge_config['codec_backend']['mode'] == 'persistent-pn-edges-v1', 'replacement mode differs')
                validate_bridge(load(prefix + 'return-observer-cold.json'), load(prefix + 'return-observer-final.json'),
                    load(prefix + 'pn-bridge-install.json'), load(prefix + 'pn-bridge-final.json'),
                    load(prefix + 'return-observer-uninstall.json'), load(prefix + 'return-observer-release.json'),
                    load(prefix + 'e2-final.json'), len(ORDERS['original-' + pair['suffix']]))
        original, replacement = pair['original'], pair['replacement']
        edges = validate_pair(load(original + '/pn-edge-matrix.json'), load(replacement + '/pn-edge-matrix.json'),
            pair['suffix'], load(original + '/private-config.json')['semantics_probe'],
            load(replacement + '/private-config.json')['semantics_probe'])
        results.append(dict(sequence=pair['suffix'], cases=len(edges['cases']), native_returns_equal=True,
            raw_transitions_and_commits_equal=True, xml_semantic_restore_equal=True,
            complete_trajectories=4, snapshots_per_trajectory=3602,
            trajectory_sha256=EXPECTED_TRAJECTORY['trajectory_sha256'], platform_asset_events=0,
            provider_failures=0, live_assets=0))
    return dict(schema='e2.1m-bounded-summary.v1', status='bounded_replacement_verified', pairs=results,
        attachment_count=len(manifest['files']), strict_gate='BLOCKED', removed_keys=0,
        no_vm_verified=False, android_free_verified=False, training_admissible=False,
        original_xml_writer_replaced=False, backup_delivery='unknown', concurrency_equivalence='unknown',
        unsupported=UNSUPPORTED, abnormal_state_followed_by_complete_match='verified for these restored fixture sequences only')


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
