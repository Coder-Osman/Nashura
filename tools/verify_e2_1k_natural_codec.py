"""Verify bounded natural PN native codec integration with original Java XML retained."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

from e2_1_commit_events import commit_counts, delta, parse_events
from e2_1k_path_trace import parse_path
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources
from verify_e2_1d_evidence import validate_cold
from verify_e2_1i_cold_pn_bridge import natural_values, pn_rows


RUNS = ['baseline-final', 'original-final', 'replacement-final-3', 'replacement-final-4']
SOURCES = ['verify_e2_1k_natural_codec.py', 'e2_1k_path_trace.py', 'e2_1_partial_method_trace.py',
    'e2_1_method_trace.py', 'e2_1_pn_native_backend.js', 'e2_1_pn_bridge.js',
    'e2_1_pn_bridge.c', 'e2_1_pn_bridge.h', 'e2_1_pn_derive.c', 'e2_1_pn_derive.h',
    'e2_1_pn_codec.c', 'e2_1_pn_codec.h', 'E21PreferenceObserver.java', 'e2_1_return_observer.js',
    'run_e2_observed.py', 'e2_1_config_probe.js', 'e2_1_commit_events.py',
    'verify_e2_1i_cold_pn_bridge.py', 'verify_e2_1h_pn_bridge.py', 'verify_e2_1d_evidence.py',
    'verify_e0_snapshots.py', 'verify_e1_gate.py', 'verify_e2_gate.py',
    'e0_dependency_observer.js', 'e2_jni_config_observer.js', 'e1_resource_redirect.js', 'e1_fmod_binding.js']
SCOPE = 'first-native-bridge PN control and native codec; original store constructors/XML/backup and JavaVM retained'
ROUTE = 'native-state-machine-native-codec-java-xml'
UNSUPPORTED = ['missing', 'legacy-plain', 'empty', 'malformed', 'commit-false-equivalence', 'concurrency']


def bridge_state(row, retired):
    require(row['schema'] == 'e2.1k-cold-pn-codec-bridge.v1' and row['scope'] == SCOPE
            and row['installed'] is not retired and row['retired'] is retired
            and row['errors'] == [] and row['retained_allocations'] == 10
            and type(row['fallbacks']) is int and row['fallbacks'] >= 0, 'native codec bridge lifecycle differs')
    require(row['strict_gate'] == 'BLOCKED' and row['no_vm_verified'] is False
            and row['original_xml_writer_replaced'] is False
            and row['backup_delivery'] == row['concurrency_equivalence'] == 'unknown', 'unsupported promotion')
    native = row['native_backend']
    require(native['schema'] == 'e2.1k-natural-pn-native-codec.v1' and native['started'] is True
            and native['ready'] is not retired and native['retired'] is retired
            and native['derivations'] == 1 and native['errors'] == []
            and native['xml_backend'] == 'original SharedPreferences'
            and native['original_store_constructors'] == 'retained'
            and native['unsupported'] == UNSUPPORTED and native['no_vm_verified'] is False
            and native['strict_gate'] == 'BLOCKED', 'native codec lifecycle/coverage differs')


def validate_replacement(initial, final, installed, bridge, observed, uninstall, release, path):
    require(initial['references'] == dict(allocated_globals=1, deleted_globals=0, retired=False)
            and final['references'] == dict(allocated_globals=1, deleted_globals=1, retired=True)
            and uninstall == {'result': 'uninstalled\t2'} and release == {'result': 'released\t6'},
            'observer/store references not retired')
    first, cold = initial['cold'], final['cold']
    for state in [first, cold]:
        require(state['armed'] is True and state['attempted'] is True and state['installed'] is True
                and state['installing'] is False and state['reentrant_entries'] == 0
                and state['errors'] == [] and state['install_result'] == 'installed\t2', 'cold native install failed')
        entry = state['install_entry']
        require(entry['entry'] == 1 and entry['operation'] == 'read' and entry['offset'] == 21022348
                and type(entry['thread']) is int and entry['thread'] > 0, 'first native bridge provenance differs')
    require(first['windows'] == cold['windows'] and len(cold['windows']) == 2
            and first['external_windows'] == 1 and cold['external_windows'] == 3
            and cold['entries'] >= first['entries'], 'natural/external windows differ')
    bridge_state(installed, False)
    bridge_state(bridge, True)
    events = bridge['events']
    require(len(installed['events']) == 3 and events[:3] == installed['events'] and len(events) == 5
            and bridge['fallbacks'] >= installed['fallbacks']
            and cold['entries'] == bridge['fallbacks'] + len(events), 'unattributed bridge entries')
    before, after = natural_values(observed)
    specifications = [('read', '0x1394198', 'configuration', 'PreferenceRead', '0x76cae0'),
                      ('write', '0x13942f0', 'cold_ready_wait', 'PreferenceWrite', '0x76cfcc')]
    bases, windows = set(), []
    for index, (window, spec) in enumerate(zip(cold['windows'], specifications)):
        operation, caller, phase, upper, upper_caller = spec
        event = events[index]
        require(window['operation'] == event['operation'] == operation and window['caller'] == caller
                and event['caller'] == 'libg.so+' + caller and window['bridge_event'] == index
                and window['thread'] == event['thread'] and type(event['thread']) is int
                and event['thread'] > 0 and window.get('completion') == 'original callsite return instruction',
                'natural bridge callsite or thread differs')
        base, site = int(event['module_base'], 16), int(event['return_site'], 16)
        require(base > 0 and base % 4096 == 0 and site == base + int(caller, 16)
                and window['module_base'] == event['module_base']
                and window['return_site'] == event['return_site'], 'natural return instruction differs')
        bases.add(base)
        delegated = delta(window['before'], window['after'])
        read_ops = ['contains', 'contains', 'getString']
        operations = [('p', name) for name in read_ops] if not index else [
            (store, name) for store in ['o', 'p'] for name in read_ops + ['edit', 'putString', 'commit']]
        expected_order = [(store, name + ':' + edge) for store, name in operations for edge in ['enter', 'exit']]
        commits = {} if not index else {'o:true': 1, 'p:true': 1}
        require([(row['store'], row['operation']) for row in delegated] == expected_order
                and len({row['thread'] for row in delegated}) == 1 and commit_counts(delegated) == commits,
                'raw XML operation order or actual commit differs')
        rows = pn_rows(observed, upper)
        require(len(rows) == 1 and rows[0]['phase'] == phase
                and rows[0]['caller'] == 'libg.so+' + upper_caller and rows[0]['thread'] == event['thread'],
                'independent upper PN phase/caller/thread differs')
        receipt = dict(reads=1, writes=0, changed=0, backup_method_returns=0, failure=0) if not index else dict(
            reads=2, writes=2, changed=3, backup_method_returns=1, failure=0)
        require(event['receipt'] == receipt and event['status'] == 0 and event['route'] == ROUTE
                and (event['result'] == before if not index else event['input'] == after), 'native transition differs')
        windows.append(dict(operation=operation, independently_observed_phase=phase, commits=commits))
    require(len(bases) == 1 and cold['windows'][0]['entry'] < cold['windows'][1]['entry'], 'call order differs')
    for event, phase in zip(events[2:], ['cold_ready_wait', 'after_reset', 'final_reset_state']):
        require(event['operation'] == 'read' and event['caller'] == 'external-probe' and event['phase'] == phase
                and event['result'] == after and event['status'] == 0 and event['route'] == ROUTE
                and event['receipt'] == dict(reads=1, writes=0, changed=0, backup_method_returns=0, failure=0),
                'explicit readback attribution differs')
    native = bridge['native_backend']
    require(native['events'][:len(installed['native_backend']['events'])] == installed['native_backend']['events']
            and installed['native_backend']['transforms'] == 8 and native['transforms'] == 10,
            'native transform prefix/count differs')
    operations = [('read', 1), ('read', 0), ('write', 0), ('read', 1), ('write', 1)] + [('read', 1)] * 3
    require(len(native['events']) == 9 and native['events'][0] == dict(operation='initialize',
            thread=events[0]['thread'], status='ready', identity_source='original-app-context Settings.Secure',
            original_store_constructors='retained'), 'native identity acquisition differs')
    for index, (row, (operation, store)) in enumerate(zip(native['events'][1:], operations)):
        require(row['operation'] == operation and row['store'] == store and row['status'] == 'returned'
                and row['thread'] == (events[0]['thread'] if index < 5 else events[index - 3]['thread']),
                'native raw backend operation/thread differs')
    require(path['named_cipher_calls_inside_roots'] == path['named_old_store_calls_inside_roots'] == 0,
            'named old PN backend inside native raw roots')
    return dict(natural_windows=windows, explicit_reads=3, derivations=1, native_transforms=10,
                raw_art_roots=len(path['roots']), raw_named_cipher_calls=0, raw_named_old_store_calls=0,
                global_named_cipher_calls=path['global_named_counts'].get('javax.crypto.Cipher.doFinal([B)[B', 0),
                opaque_method_ids=len(path['unnamed_methods']), exhaustive_method_coverage=False,
                non_pn_original_bridge_fallbacks=bridge['fallbacks'], released_java_references=6,
                allocated_global_references=1, deleted_global_references=1)


def validate_xml(readback, before, after, expected):
    require(readback == dict(route='original c.a.j via retained store objects', values=[expected, expected]),
            'original Java per-store readback differs')
    require(len(before) == len(after) == 2 and before == after, 'readback changed original XML bytes')
    for raw in before:
        root = ET.fromstring(raw)
        require(root.tag == 'map' and all(row.get('name') != 'PN_POPUP' for row in root),
                'legacy plain PN entry or unexpected XML')
    return dict(stores=2, original_java_readback_equal=True, xml_bytes_unchanged_by_readback=True)


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1k-private-manifest.v1' and manifest['runs'] == RUNS, 'wrong K manifest')
    base = base.resolve()
    for relative, expected in manifest['files'].items():
        path, target = Path(relative), base / relative
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
        require(hashlib.sha256(raw(record['path'])).hexdigest() == record['sha256'], 'self-built artifact differs')
    reproduction = load('build-reproduction.json')
    require(reproduction['status'] == 'matched' and len(reproduction['commands']) == 3
            and all(row['exit_code'] == 0 for row in reproduction['commands'])
            and reproduction['identities'] == {name: row['sha256'] for name, row in artifacts.items()},
            'native library/observer reproduction differs')
    for name in ['e2_1_pn_bridge.c', 'e2_1_pn_bridge.h', 'e2_1_pn_codec.c', 'e2_1_pn_codec.h',
                 'e2_1_pn_derive.c', 'e2_1_pn_derive.h', 'E21PreferenceObserver.java']:
        require(reproduction['sources'][name] == manifest['files'][manifest['verification_sources'][name]],
                'rebuilt source differs')
    require(load('guest-artifacts-after.json') == dict(exit_code=0, identities={row['guest']: row['sha256']
                for row in artifacts.values()}), 'final guest artifact identity differs')
    origin, material = load('material-origin.json'), load('private-material.json')
    dex = raw('private-source/installed-primary.dex')
    require(hashlib.sha256(dex).hexdigest() == origin['dex_sha256']
            == 'ef1ac708f6e836daa925afc50d3bf3b139213b3a72323a354cc2e9493b50f608'
            and origin['material_sha256'] == manifest['files']['private-material.json'], 'private material source differs')
    require(struct.unpack_from('<HHI', dex, origin['table_header_offset']) == (0x300, 4, 24)
            and origin['table_offset'] == origin['table_header_offset'] + 8
            and material['table'] == list(struct.unpack_from('<24i', dex, origin['table_offset'])), 'signed table differs')
    line = raw('private-source/installed-primary.dexdump.txt').decode().splitlines()[origin['iv_dump_line']]
    iv = re.search(r'const-string.*?, "(.*?)"', line)[1].encode()
    require(iv in dex and len(iv) == 35 and material['iv_source'] == list(iv), 'explicit IV source differs')
    original, results, processes = load(RUNS[0] + '/identity-before.json'), [], set()
    for index, run in enumerate(RUNS):
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        require(config['mode'] == 'original' and not any(key in config for key in
                ['replacement', 'pn_codec', 'pn_bridge_matrix', 'pn_derivation', 'java_oracle']), 'unrelated probe enabled')
        require(config == load(run + '.config.json') and load(prefix + 'run-start.json')['config_sha256']
                == manifest['files'][run + '.config.json'], 'run configuration identity differs')
        completed_run(base, run, load(prefix + 'run-finished.json'),
            [json.loads(row) for row in raw(prefix + 'messages.jsonl').splitlines() if row.strip()])
        owned = load(prefix + 'owned-process.json')
        require(owned['pid'] not in processes and owned['stat'].startswith(str(owned['pid']) + ' '), 'process reused')
        processes.add(owned['pid'])
        identity = load(prefix + 'identity-before.json')
        require(len(identity['files']) == 9 and identity == load(prefix + 'identity-after.json')
                and identity['files'] == original['files'] == config['guest_identity']
                and identity['update_manifest_sha256'] == original['update_manifest_sha256']
                == config['update_identity_sha256'], 'frozen input identity differs')
        for record in artifacts.values():
            require(identity['files'][record['guest']] == record['sha256'], 'actual native library/DEX differs')
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
                'native action/snapshot/restore/reset/terminal controls differ')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        cleanup = load(prefix + 'cleanup.json')
        require(cleanup['action'] == 'force-stop completed owned attempt' and cleanup['pid'] == owned['pid']
                and cleanup['stat'].split(') ', 1)[1].split()[19] == owned['stat'].split(') ', 1)[1].split()[19],
                'owned process cleanup differs')
        result = dict(run=run, **EXPECTED_TRAJECTORY, platform_asset_events=0, provider_failures=0, live_assets=0)
        if index:
            settings = config['return_observer']
            require(settings['install_at'] == 'first-native-bridge' and settings['release_after_uninstall'] is True
                    and settings['dex_path'] == artifacts['dex']['guest'], 'observer deployment differs')
            parse_events(raw(prefix + 'return-observer-events.tsv').decode())
            observed = load(prefix + 'e2-final.json')
            initial, final = load(prefix + 'return-observer-cold.json'), load(prefix + 'return-observer-final.json')
            uninstall, release = load(prefix + 'return-observer-uninstall.json'), load(prefix + 'return-observer-release.json')
            path = parse_path(raw(prefix + 'pn-path.trace'), index >= 2)
            require(load(prefix + 'pn-path-trace-state.json') == dict(started=True, stopped=True),
                    'natural trace did not complete')
            if index == 1:
                require('pn_bridge' not in config, 'original reference replaced')
                result['cold'] = validate_cold(initial, final, observed, uninstall, release)
                natural_values(observed)
                require(path['named_cipher_calls_inside_roots'] > 0 and path['named_old_store_calls_inside_roots'] > 0,
                        'original positive trace control absent')
                result['art_scope'] = 'all named original get/store roots during window; not PN-only'
            else:
                native_config = config['pn_bridge']['codec_backend']
                require(config['pn_bridge']['install_at'] == 'first-native-bridge'
                        and config['pn_bridge']['mode'] == 'replacement'
                        and config['pn_bridge']['library'] == artifacts['library']['guest']
                        and settings['enable_pn_backend'] is True and config['pn_native_readback'] is True
                        and native_config == dict(material, mode='present-ciphertext-v1', package=config['package'],
                                                  crypto_library='/system/lib64/libcrypto.so'), 'native contract differs')
                result['cold'] = validate_replacement(initial, final, load(prefix + 'pn-bridge-install.json'),
                    load(prefix + 'pn-bridge-final.json'), observed, uninstall, release, path)
                result['xml'] = validate_xml(load(prefix + 'original-java-readback.json'),
                    [raw(prefix + f'natural-pn-xml/store-{store}-before.xml') for store in range(2)],
                    [raw(prefix + f'natural-pn-xml/store-{store}-after.xml') for store in range(2)], natural_values(observed)[1])
        else:
            require('pn_bridge' not in config and 'return_observer' not in config, 'baseline mutation')
        results.append(result)
    require(natural_values(load(RUNS[2] + '/e2-final.json'))[1]
            == natural_values(load(RUNS[3] + '/e2-final.json'))[0], 'independent process restart readback differs')
    require(load('cleanup-result.json') == dict(status='owned_instance_released',
                preserved_historical_adb=True, listener_count=0), 'exclusive instance not released')
    return dict(schema='e2.1k-bounded-summary.v1', status='bounded_evidence_verified', runs=results,
        attachment_count=len(manifest['files']), independent_process_readback_equal=True,
        pn_native_replacement='natural control, explicit-source derivation and AES; Java identity/store/XML/backup retained',
        strict_gate='BLOCKED', removed_keys=0, original_xml_writer_replaced=False, no_vm_verified=False,
        android_free_verified=False, training_admissible=False, tenfold_speedup_verified=False,
        unsupported=UNSUPPORTED, backup_delivery='unknown', concurrency_equivalence='unknown',
        unresolved=['non-Java identity acquisition and original store construction', 'XML persistence and backup delivery',
                    'missing/legacy/empty/malformed/commit-false equivalence', 'concurrency and crash durability',
                    'opaque method identities and 46-key removability'])


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
