"""Verify bounded original Cipher/XML interoperability, never infer bridge replacement."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from e2_1_commit_events import commit_counts, delta
from e2_1_pn_codec import PNCodec
from e2_1_pn_codec_matrix import TEXTS
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources


SOURCES = ['E21PreferenceObserver.java', 'e2_1_return_observer.js', 'e2_1_config_probe.js',
           'e2_1_pn_codec.c', 'e2_1_pn_codec.h', 'e2_1_pn_codec.py', 'e2_1_pn_codec_matrix.py',
           'e2_1_pn_codec_probe.js', 'e2_1_commit_events.py', 'run_e2_observed.py',
           'verify_e2_1g_pn_codec.py', 'verify_e0_snapshots.py', 'verify_e1_gate.py', 'verify_e2_gate.py']
RUNS = ['baseline-current', 'codec-scoped-1', 'codec-scoped-2']


def xml_entries(record):
    require(set(record) == {'sha256', 'bytes_hex'}, 'XML attachment fields differ')
    raw = bytes.fromhex(record['bytes_hex'])
    require(raw.hex() == record['bytes_hex'] and hashlib.sha256(raw).hexdigest() == record['sha256'],
            'XML bytes or hash differ')
    require(b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper(), 'XML declarations rejected')
    root = ET.fromstring(raw)
    require(root.tag == 'map' and not root.attrib, 'original preference XML root differs')
    result = {}

    def value(node):
        return (node.tag, tuple(sorted(node.attrib.items())), node.text or '', tuple(value(child) for child in node))

    for child in root:
        name = child.get('name')
        require(name is not None and name not in result, 'duplicate or unnamed XML preference')
        result[name] = value(child)
    return result


def validate_matrix(matrix, final, profile, host):
    require(matrix['schema'] == 'e2.1-pn-codec-matrix.v1' and matrix['restored'] is True
            and matrix['restore_receipt'] == 'pn-restored\t2' and matrix['cleanup_errors'] == [],
            'PN restore or codec cleanup failed')
    require(matrix['original_bridge_replaced'] is False and matrix['original_xml_writer_replaced'] is False
            and matrix['backup_delivery'] == 'unknown' and matrix['no_vm_verified'] is False,
            'unsupported codec promotion')
    require(final == dict(schema='e2.1-pn-native-codec.v1', calls=80, closed=True,
            bridge_replacement=False, original_xml_writer=False, no_vm_verified=False)
            and matrix['native_close'] == dict(closed=True, calls=80), 'guest codec lifecycle differs')
    keys = [bytes.fromhex(item) for item in profile['keys']]
    iv = bytes.fromhex(profile['iv'])
    require(len(keys) == 2 and all(len(key) == 32 for key in keys) and len(iv) == 16, 'private profile differs')
    info = matrix['original_profile']
    require(len(info) == 2, 'original cipher metadata missing')
    for store, row in enumerate(info):
        require(row['store'] == store and row['charset'] == 'UTF-8', 'original charset/store differs')
        for name in ['a', 'b', 'c']:
            expected = 'AES/ECB/PKCS5Padding' if name == 'c' else 'AES/CBC/PKCS5Padding'
            require(row[name]['algorithm'] == expected and row[name]['provider'] == 'AndroidOpenSSL'
                    and base64.b64decode(row[name]['iv'], validate=True) == (b'' if name == 'c' else iv),
                    'original algorithm/provider/IV differs')
    before = [xml_entries(item) for item in matrix['original_xml_before']]
    restored = [xml_entries(item) for item in matrix['restored_xml']]
    require(len(before) == len(restored) == 2 and before == restored, 'original XML not fully restored')
    encoded_keys = [host.encode('PN_POPUP', key) for key in keys]

    def check_xml(records, store, ciphertext):
        require(len(records) == 2, 'two actual XML files required')
        current = [xml_entries(item) for item in records]
        for index, entries in enumerate(current):
            encoded_key = encoded_keys[index]
            require({key: value for key, value in entries.items() if key != encoded_key}
                    == {key: value for key, value in before[index].items() if key != encoded_key},
                    'unrelated preference changed')
            if index == store:
                expected = None if ciphertext is None else ('string', (('name', encoded_key),), ciphertext, ())
                require(entries.get(encoded_key) == expected, 'actual XML ciphertext/deletion differs')

    rows = matrix['cases']
    require([(row['store'], row['ordinal']) for row in rows]
            == [(store, ordinal) for store in range(2) for ordinal in range(len(TEXTS))], 'codec case set/order differs')
    for row in rows:
        store, ordinal = row['store'], row['ordinal']
        text, key = TEXTS[ordinal], keys[store]
        require(row['utf8_bytes'] == len(text.encode()) and row['injected_source'] == 'guest-native-codec'
                and base64.b64decode(row['original_read_b64'], validate=True) == text.encode(),
                'original UTF-8 readback or injected source differs')
        require([item['purpose'] for item in row['checks']] == ['key', 'value'], 'both cipher modes required')
        for item in row['checks']:
            ciphertext = base64.b64decode(item['encoded'], validate=True)
            require(base64.b64encode(ciphertext).decode() == item['encoded'], 'noncanonical base64')
            expected = host.transform(text.encode(), key, iv if item['purpose'] == 'value' else None)
            require(ciphertext == expected and item['host_sha256'] == hashlib.sha256(expected).hexdigest()
                    and item['guest'] == dict(status=0, output=list(expected))
                    and item['reverse'] == dict(status=0, output=list(text.encode()))
                    and host.transform(expected, key, iv if item['purpose'] == 'value' else None,
                                       encrypt=False) == text.encode(), 'original/host/guest cipher bytes differ')
        expected = row['checks'][1]['encoded'] if text else None
        require(row['original_state'].get('encoded') == expected, 'original raw state differs')
        check_xml(row['xml'], store, expected)
    malformed = matrix['malformed']
    cases = [('bad-base64', '%%%'), ('short-block', base64.b64encode(b'bad').decode()),
             ('zero-block', base64.b64encode(bytes(16)).decode())]
    require([(row['store'], row['case'], row['encoded']) for row in malformed]
            == [(store, name, encoded) for store in range(2) for name, encoded in cases],
            'malformed case set differs')
    for row in malformed:
        require(row['original_read_b64'] == '' and row['after'].get('encoded') is None,
                'original malformed cleanup differs')
        events = delta(row['events_before'], row['events_after'])
        require(len({event['thread'] for event in events}) == 1
                and commit_counts(events) == {('o' if row['store'] == 0 else 'p') + ':true': 1},
                'malformed cleanup commit provenance differs')
        check_xml(row['xml'], row['store'], None)
    return dict(texts_per_store=10, stores=2, cipher_comparisons=40, native_transforms=80,
                original_reads=20, malformed_cleanup_cases=6, xml_restored=True,
                format_scope='explicit-key ECB/CBC bytes and original SharedPreferences reader/writer only',
                backup_delivery='unknown', original_bridge_replaced=False, original_xml_writer_replaced=False)


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1g-private-manifest.v1', 'wrong manifest schema')
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
    require(set(artifacts) == {'host', 'guest', 'dex'}, 'artifact set differs')
    hashes = {name: hashlib.sha256(raw(item['path'])).hexdigest() for name, item in artifacts.items()}
    build = load('build-reproduction.json')
    require(build['exit_code'] == 0 and build['rebuilt_sha256'] == hashes['guest']
            and all(flag in build['argv'] for flag in ['-shared', '-fPIC', '-Werror'])
            and build['argv'][0].endswith('/aarch64-linux-android30-clang'), 'guest build not reproduced')
    for name in ['e2_1_pn_codec.c', 'e2_1_pn_codec.h']:
        require(build['sources'][name] == manifest['files'][manifest['verification_sources'][name]], 'build source differs')
    build_commands = load('build-commands.json')
    require(len(build_commands) == 12 and all(row['exit_code'] == 0 for row in build_commands), 'codec/observer build failed')
    require(load('build-identities.json') == hashes, 'build artifact identity differs')
    original = load(RUNS[0] + '/identity-before.json')
    require(len(original['files']) == 9, 'baseline identity set differs')
    processes, results, matrices = set(), [], []
    for run in RUNS:
        prefix = run + '/'
        config = load(prefix + 'private-config.json')
        require(config['mode'] == 'original' and 'replacement' not in config, 'original bridge must remain installed')
        completed_run(base, run, load(prefix + 'run-finished.json'),
                      [json.loads(line) for line in raw(prefix + 'messages.jsonl').splitlines() if line.strip()])
        process = load(prefix + 'owned-process.json')
        require(process['pid'] not in processes and process['stat'].startswith(str(process['pid']) + ' '),
                'independent process identity missing')
        processes.add(process['pid'])
        identity = load(prefix + 'identity-before.json')
        require(identity == load(prefix + 'identity-after.json'), 'frozen input identity changed')
        expected = dict(original['files'])
        if run != RUNS[0]:
            codec = config['pn_codec']
            expected.update(manifest['crypto_identity'])
            require(codec['library'] == artifacts['guest']['guest']
                    and config['return_observer']['dex_path'] == artifacts['dex']['guest']
                    and config['return_observer']['enable_pn_codec'] is True, 'executed artifacts differ')
            require(codec['crypto_library'] in manifest['crypto_identity']
                    and config['local_identity'][codec['host_library']] == hashes['host'], 'crypto identity missing')
            configured = load(prefix + 'pn-codec-configure.json')
            require(configured['provider_path'] == codec['crypto_library']
                    and configured['native_codec_path'] == codec['library'], 'actual codec provider differs')
            require(load(prefix + 'return-observer-uninstall.json')['result'] == 'uninstalled\t2'
                    and load(prefix + 'return-observer-release.json')['result'] == 'released\t6'
                    and load(prefix + 'return-observer-final.json')['references']
                    == dict(allocated_globals=1, deleted_globals=1, retired=True), 'observer references not retired')
            matrix = load(prefix + 'pn-codec-matrix.json')
            profile = load(manifest['profile'])
            require(matrix['profile_sha256'] == manifest['files'][manifest['profile']], 'profile identity differs')
            before_records = load(prefix + 'pn-codec-original-xml-before.json')
            matrix['original_xml_before'] = before_records
            xml_sets = [('before', before_records), ('restored', matrix['restored_xml'])]
            xml_sets += [(f's{row["store"]}-t{row["ordinal"]}', row['xml']) for row in matrix['cases']]
            xml_sets += [(f's{row["store"]}-{row["case"]}', row['xml']) for row in matrix['malformed']]
            for label, records in xml_sets:
                for index, record in enumerate(records):
                    require(raw(prefix + f'codec-xml-{label}/store-{index}.xml') == bytes.fromhex(record['bytes_hex']),
                            'pulled original XML attachment differs')
            host = PNCodec(base / artifacts['host']['path'], codec['host_crypto_library'])
            try:
                matrices.append(dict(run=run, **validate_matrix(matrix, load(prefix + 'pn-codec-final.json'), profile, host)))
            finally:
                host.close()
            for name in SOURCES[:8]:
                require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                        'exercised codec source differs: ' + name)
        else:
            require('pn_codec' not in config and 'return_observer' not in config, 'baseline has codec mutation')
        require(identity['files'] == config['guest_identity'] == expected
                and identity['update_manifest_sha256'] == config['update_identity_sha256']
                == original['update_manifest_sha256'], 'original inputs or update identity differ')
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
            require(manifest['files'].get(prefix + 'sources/' + name) == digest, 'archived script identity differs')
        for tick in range(3602):
            raw(prefix + f'snapshots/tick-{tick:06d}.bin')
        comparison = verify(base / RUNS[0] / 'snapshots', base / run / 'snapshots',
                            load(RUNS[0] + '/trace.json'), load(prefix + 'trace.json'))
        require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'native trajectory differs')
        require(controls(load(prefix + 'control-receipts.json')) == controls(load(RUNS[0] + '/control-receipts.json')),
                'native action/snapshot/restore/reset/terminal differs')
        resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
        results.append(dict(run=run, **{key: comparison[key] for key in EXPECTED_TRAJECTORY},
                            platform_asset_events=0, provider_failures=0, live_assets=0))
    return dict(schema='e2.1g-bounded-summary.v1', status='bounded_evidence_verified', strict_gate='BLOCKED',
                attachment_count=len(manifest['files']), runs=results, matrices=matrices,
                pn_native_replacement=False, original_xml_writer_replaced=False,
                no_vm_verified=False, android_free_verified=False, training_admissible=False,
                tenfold_speedup_verified=False, removed_keys=0,
                unresolved=['native PN bridge integration and original XML writer handoff', 'backup delivery',
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
