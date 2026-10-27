"""Verify post-trajectory PN edge evidence without promoting targeted or no-VM gates."""

import argparse
import base64
import hashlib
import json
from pathlib import Path

from e2_1_commit_events import commit_counts, delta, parse_events
from e2_1_pn_edge_matrix import CASES, raw_fixture
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources
from verify_e2_1g_pn_codec import xml_entries


ORDERS = {
    'original-edges': [row[0] for row in CASES],
    'original-malformed-first': ['malformed-preferred', 'missing-older'],
    'original-padding-first': ['bad-padding', 'missing-older', 'missing-preferred'],
    'original-short-state': ['short-block', 'missing-older', 'missing-preferred'],
    'original-padding-state': ['bad-padding', 'write-over-legacy', 'missing-older', 'missing-preferred'],
}
DIRECT_ORDER = [(0, name) for name in ['valid-before', 'short-block', 'valid-after-short',
    'valid-after-short-again']] + [(1, name) for name in ['valid-before', 'bad-padding',
    'valid-after-padding', 'valid-after-padding-again']]
SCOPE = 'explicit post-trajectory raw fixtures; actual native PN entry'
RUNS = ['baseline', *ORDERS, 'original-direct-cipher', 'original-stream-diff-3', 'original-stream-diff-4']
SOURCES = ['verify_e2_1l_edges.py', 'e2_1_pn_edge_matrix.py', 'e2_1_commit_events.py',
           'verify_e0_snapshots.py', 'verify_e1_gate.py', 'verify_e2_gate.py', 'verify_e2_1g_pn_codec.py',
           'e2_1_pn_stream.c', 'e2_1_pn_stream.h', 'e2_1_pn_stream_probe.js',
           'e2_1_pn_derive.c', 'e2_1_pn_derive.h', 'e2_1_pn_codec.h', 'E21PreferenceObserver.java']


def expected_case(label, name):
    counts, changes, value = {}, ['missing', 'missing'], ''
    if name == 'missing-preferred':
        counts, changes, value = {'p:true': 1}, ['same', 'changed'], 'older-value'
    elif name == 'missing-older':
        changes, value = ['missing', 'same'], 'preferred-value'
    elif name == 'legacy-cipher-conflict':
        changes, value = ['same', 'same'], 'preferred-value'
    elif name in {'legacy-empty-cipher', 'empty-raw', 'empty-cipher', 'bad-base64',
                  'short-block', 'bad-padding', 'malformed-preferred', 'legacy-malformed', 'write-empty'}:
        counts = {'o:true': 1, 'p:true': 1}
    elif name in {'write-over-legacy', 'write-over-malformed'}:
        amount = 2 if name == 'write-over-malformed' else 1
        counts, changes = {'o:true': amount, 'p:true': amount}, ['changed', 'changed']
    if label == 'original-malformed-first' and name == 'malformed-preferred':
        counts, changes, value = {'p:true': 2}, ['same', 'changed'], 'older-value'
    if label in {'original-padding-first', 'original-short-state', 'original-padding-state'}:
        if name in {'missing-older', 'missing-preferred'}:
            counts = {'p:true' if name == 'missing-older' else 'o:true': 1}
            changes, value = ['missing', 'missing'], ''
    return counts, changes, value


def validate_matrix(matrix, label, config):
    require(matrix['schema'] == 'e2.1l-pn-edge-matrix.v1' and matrix['scope'] == SCOPE
            and matrix['replacement'] is False and matrix['restored'] is True
            and matrix['restore_receipt'] == 'pn-restored\t2', 'edge scope or restore differs')
    require(matrix['strict_gate'] == 'BLOCKED' and matrix['no_vm_verified'] is False
            and matrix['concurrency_equivalence'] == 'unknown', 'unsupported edge promotion')
    require(label in RUNS[1:], 'unknown diagnostic run')
    names = ORDERS.get(label, ['missing-both'])
    require([row['case'] for row in matrix['cases']] == names, 'edge case order differs')
    require('case_order' not in matrix or matrix['case_order'] == names, 'declared case order differs')
    before = [xml_entries(row) for row in matrix['xml_before']]
    restored = [xml_entries(row) for row in matrix['xml_restored']]
    require(len(before) == len(restored) == 2 and before == restored, 'original XML semantic restore differs')
    require(all('PN_POPUP' not in row for row in before), 'unexpected initial legacy PN entry')
    encoded_keys = [set(), set()]
    parsed_xml = []
    for row in matrix['cases']:
        parsed = [xml_entries(item) for item in row['xml_after']]
        require(len(parsed) == 2, 'two XML stores required')
        parsed_xml.append(parsed)
        for store in range(2):
            keys = before[store].keys() | parsed[store].keys()
            encoded_keys[store].update(key for key in keys if key != 'PN_POPUP'
                and before[store].get(key) != parsed[store].get(key))
    require(all(len(keys) == 1 for keys in encoded_keys), 'unique changed encrypted PN key required')
    keys = [next(iter(items)) for items in encoded_keys]
    cases = {row[0]: row for row in CASES}
    summaries, prior_events = [], None
    for row, xml in zip(matrix['cases'], parsed_xml):
        name, kinds, operation, input_value = cases[row['case']]
        require(row['kinds'] == list(kinds) and row['operation'] == operation
                and len(row['fixtures']) == len(row['raw_after']) == 2, 'fixture identity differs')
        counts, changes, value = expected_case(label, name)
        native = row['native']
        require(native['case'] == name and native['operation'] == operation and native['key'] == 'PN_POPUP'
                and native['entry'] == operation + '_bridge'
                and native['entry_offset'] == config[operation + '_bridge']
                and type(native['thread']) is int and native['thread'] > 0
                and native['error_status'] == 'unknown; original bridge exposes no typed error'
                and native['exception_state'] == dict(before=False, after=False,
                    source='explicit JNIEnv.ExceptionCheck on probe thread'), 'native bridge provenance differs')
        if operation == 'read':
            require(native['return_type'] == 'EngineString16 through AArch64 x8'
                    and native['result'] == dict(value=value, length=len(value.encode()), bytes=list(value.encode())),
                    'native read differs')
        else:
            require(native['input'] == input_value and 'result' not in native
                    and native['return_type'] == 'void; raw register not interpreted as status',
                    'native void write status inferred')
        events = delta(row['events_before'], row['events_after'])
        require(len({event['thread'] for event in events}) == 1
                and commit_counts(events) == counts, 'delegated commit count/return differs')
        current = parse_events(row['events_before'])
        require(prior_events is None or current[:len(prior_events)] == prior_events,
                'edge event history changed between fixtures')
        prior_events = parse_events(row['events_after'])
        for store in range(2):
            fixture, after = row['fixtures'][store], row['raw_after'][store]
            require(set(fixture) == {'plain', 'encoded'} and set(after) <= {'plain', 'encoded'},
                    'unexpected raw preference fields')
            expected = raw_fixture(kinds[store], store, lambda unused: fixture['encoded'])
            require(fixture == expected and (kinds[store] not in {'value', 'conflict', 'empty-cipher'}
                    or isinstance(fixture['encoded'], str) and len(base64.b64decode(
                        fixture['encoded'], validate=True)) in {16, 32}), 'raw input fixture differs')
            require(after.get('plain') == fixture['plain'], 'legacy plain entry changed')
            encoded = after.get('encoded')
            change = 'missing' if encoded is None else 'same' if encoded == fixture['encoded'] else 'changed'
            require(change == changes[store], 'encoded removal/backfill transition differs')
            if encoded is not None:
                require(len(base64.b64decode(encoded, validate=True)) % 16 == 0 and encoded != '',
                        'encoded result is not nonempty CBC blocks')
            expected_xml = dict(before[store])
            for key, raw in [('PN_POPUP', fixture['plain']), (keys[store], encoded)]:
                if raw is None:
                    expected_xml.pop(key, None)
                else:
                    expected_xml[key] = ('string', (('name', key),), raw, ())
            require(xml[store] == expected_xml, 'persisted XML differs from actual raw return or unrelated preference changed')
        summaries.append(dict(case=name, commits=counts, encoded_transitions=changes,
                              pending_jni_exception=False))
    return dict(cases=summaries, restored_semantically=True, xml_order_normalization='none; entry values compared',
                scope=SCOPE, concurrency_equivalence='unknown')


def validate_direct(matrix, stream_required):
    require([(row['store'], row['case']) for row in matrix['direct_cipher']] == DIRECT_ORDER,
            'direct Cipher sequence differs')
    expected_lengths = [16, None, None, None, 16, None, 32, 16]
    mismatches, inputs = [], {}
    for index, row in enumerate(matrix['direct_cipher']):
        original = row['result']
        require(original['store'] == row['store'] and original['route'] ==
                'explicit original store Cipher.b.doFinal; mutates decoder state', 'direct Cipher provenance differs')
        amount = expected_lengths[index]
        if amount is None:
            exception = 'javax.crypto.BadPaddingException' if index == 5 else 'javax.crypto.IllegalBlockSizeException'
            require(original == dict(store=row['store'], route=original['route'], status='threw', exception=exception),
                    'original direct exception differs')
            output = b''
        else:
            output = base64.b64decode(original['output'], validate=True)
            require(original['status'] == 'returned' and original['bytes'] == len(output) == amount
                    and (amount != 16 or output == b'diagnostic-value'), 'direct original output differs')
        if stream_required:
            data = base64.b64decode(row['input'], validate=True)
            if index in {1, 5}:
                require(data == (b'bad' if index == 1 else bytes(16)), 'direct malformed input differs')
            else:
                require(len(data) == 32 and inputs.setdefault(row['store'], data) == data,
                        'valid direct inputs changed within store')
            stream = row['stream']
            require(set(stream) == {'status', 'output'} and type(stream['status']) is int
                    and isinstance(stream['output'], list)
                    and all(type(value) is int and 0 <= value <= 255 for value in stream['output']),
                    'invalid native stream output')
            same = stream['status'] == (0 if amount is not None else -4) and bytes(stream['output']) == output
            if not same:
                mismatches.append(dict(store=row['store'], case=row['case'], original_status=original['status'],
                    original_bytes=len(output), native_status=stream['status'], native_bytes=len(stream['output'])))
    if stream_required:
        require('stream_close_error' not in matrix and matrix['stream_close'] == dict(closed=True, stores=2,
                calls=[4, 4], bridge_replaced=False, no_vm_verified=False), 'native stream cleanup differs')
        require(not mismatches, 'persistent native decoder differs from original Cipher')
    return dict(calls=8, direct_original_exceptions=4, padding_recovery_bytes=[32, 16],
                native_stream_compared=stream_required, native_mismatches=mismatches,
                native_exception_class_equivalence='unknown; native error is not a Java exception type')


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1l-private-manifest.v1', 'wrong L manifest')
    base = base.resolve()
    for relative, expected in manifest['files'].items():
        target = base / relative
        require(not Path(relative).is_absolute() and '..' not in Path(relative).parts
                and not any(path.is_symlink() for path in [target, *target.parents]), 'unsafe attachment path')
        require(hashlib.sha256(target.read_bytes()).hexdigest() == expected, 'attachment hash differs: ' + relative)

    def raw(relative):
        require(relative in manifest['files'], 'unbound attachment: ' + relative)
        return (base / relative).read_bytes()

    def load(relative):
        return json.loads(raw(relative))

    for name in SOURCES:
        require(raw(manifest['verification_sources'][name]) == Path(__file__).with_name(name).read_bytes(),
                'verification source differs: ' + name)
    artifacts = load('stream-build-reproduction.json')
    require(artifacts['status'] == 'matched' and len(artifacts['commands']) == 3
            and all(row['exit_code'] == 0 for row in artifacts['commands']), 'build reproduction differs')
    require(set(artifacts['artifacts']) == {'stream', 'direct_observer'}, 'self-built artifact set differs')
    for name, digest in artifacts['sources'].items():
        require(digest == manifest['files'][manifest['verification_sources'][name]], 'compiled source differs')
    for record in artifacts['artifacts'].values():
        require(hashlib.sha256(raw(record['original'])).hexdigest()
                == hashlib.sha256(raw(record['rebuilt'])).hexdigest() == record['sha256'], 'self-built bytes differ')

    baseline = 'baseline'
    runs = manifest['runs']
    require(runs == RUNS, 'required independent runs missing')
    identities = load('baseline/identity-before.json')
    baseline_controls = controls(load('baseline/control-receipts.json'))
    summary, processes = [], set()
    for label in runs:
        require('/' not in label and label not in {'.', '..'}, 'unsafe run label')
        config = load(label + '/private-config.json')
        require(config['mode'] == 'original' and 'pn_bridge' not in config
                and 'replacement' not in config, 'original reference replaced')
        start = load(label + '/run-start.json')
        require(config == load(label + '.config.json')
                and start['config_sha256'] == manifest['files'][label + '.config.json']
                and start['runner_sha256'] == manifest['files'][label + '/sources/run_e2_observed.py'],
                'executed configuration or runner differs')
        for name, digest in load(label + '/source-identity.json').items():
            require(manifest['files'].get(label + '/sources/' + name) == digest, 'archived source differs')
        messages = [json.loads(line) for line in raw(label + '/messages.jsonl').splitlines()]
        completed_run(base, label, load(label + '/run-finished.json'), messages)
        owned = load(label + '/owned-process.json')
        require(owned['pid'] not in processes, 'independent process required')
        processes.add(owned['pid'])
        cleanup = load(label + '/cleanup.json')
        require(cleanup['pid'] == owned['pid'] and cleanup['action'] == 'force-stop completed owned attempt'
                and cleanup['stat'].split(') ', 1)[1].split()[19]
                == owned['stat'].split(') ', 1)[1].split()[19], 'owned process cleanup differs')
        before, after = load(label + '/identity-before.json'), load(label + '/identity-after.json')
        require(before == after and before['files'] == config['guest_identity']
                and before['update_manifest_sha256'] == identities['update_manifest_sha256']
                and all(before['files'].get(path) == digest for path, digest in identities['files'].items()),
                'frozen APK/libg/FMOD/probe/provider/update identity differs')
        commands = [json.loads(line) for line in raw(label + '/commands.jsonl').splitlines()]
        updates = [row for row in commands if row['argv'][-1].startswith('find ')
                   and 'update -type f -exec sha256sum' in row['argv'][-1]]
        require(len(updates) == 2 and all(row['code'] == 0 and len(row['stdout'].splitlines()) == 990
                and hashlib.sha256(row['stdout'].strip().encode()).hexdigest() == before['update_manifest_sha256']
                for row in updates), '990-file before/after update identity missing')
        require(controls(load(label + '/control-receipts.json')) == baseline_controls, 'native lifecycle differs')
        for path in (base / label / 'snapshots').iterdir():
            require(str(path.relative_to(base)) in manifest['files'], 'unbound snapshot')
        comparison = verify(base / baseline / 'snapshots', base / label / 'snapshots',
            load('baseline/trace.json'), load(label + '/trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'complete trajectory differs')
        resources(load(label + '/e0-final.json'), load(label + '/resource-final.json'), load(label + '/fmod-final.json'))
        row = dict(run=label, snapshot_count=3602, trajectory_sha256=comparison['trajectory_sha256'],
                   native_controls_equal=True, asset_platform_events=0, frozen_inputs_unchanged=True)
        if label != baseline:
            matrix = load(label + '/pn-edge-matrix.json')
            row['edges'] = validate_matrix(matrix, label, config['semantics_probe'])
            if 'direct_cipher' in matrix:
                required = label != 'original-direct-cipher'
                row['direct'] = validate_direct(matrix, required)
                if required:
                    require(config.get('pn_direct_cipher') is True and config.get('pn_edge_matrix') is True
                            and config['return_observer'].get('enable_pn_derivation') is True,
                            'persistent diagnostic configuration differs')
                    for artifact, guest in [('stream', config['pn_stream']['library']),
                                            ('direct_observer', config['return_observer']['dex_path'])]:
                        require(before['files'][guest] == artifacts['artifacts'][artifact]['sha256'],
                                'actual stream/observer bytes differ')
                    for name in ['e2_1_pn_stream.c', 'e2_1_pn_stream.h', 'e2_1_pn_derive.c',
                                 'e2_1_pn_derive.h', 'e2_1_pn_codec.h', 'e2_1_pn_stream_probe.js',
                                 'E21PreferenceObserver.java']:
                        require(raw(label + '/sources/' + name) == raw(manifest['verification_sources'][name]),
                                'stream executed source differs')
                    require(load(label + '/pn-stream-configure.json') == dict(schema='e2.1l-persistent-decoder.v1',
                            stores=2, reset='real EVP context reinitialized only after successful final',
                            error='real EVP state retained on failed final', bridge_replaced=False, no_vm_verified=False),
                            'native stream configuration receipt differs')
                    require(load(label + '/pn-stream-final.json') == dict(schema='e2.1l-persistent-decoder-state.v1',
                            stores=2, calls=[4, 4], closed=True, bridge_replaced=False, no_vm_verified=False),
                            'final native decoder lifecycle differs')
        summary.append(row)
    return dict(schema='e2.1l-bounded-evidence-summary.v1', status='bounded_diagnostic_verified', runs=summary,
                attachment_count=len(manifest['files']),
                strict_gate='BLOCKED', removed_keys=0, no_vm_verified=False, android_free_verified=False,
                training_admissible=False, abnormal_state_followed_by_complete_match='not tested',
                direct_cipher_scope='serial explicit Cipher/EVP differential after complete trajectory; not natural replacement',
                concurrency_equivalence='unknown')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(json.loads(args.manifest.read_text()), args.manifest.parent)
    result['private_manifest_sha256'] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    with args.output.open('x') as target:
        json.dump(result, target, indent=2)
        target.write('\n')
    print(json.dumps({key: result[key] for key in ['status', 'strict_gate', 'attachment_count']}))


if __name__ == '__main__':
    main()
