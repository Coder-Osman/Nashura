"""Verify bounded explicit-source PN derivation; original cold initialization remains Java."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import struct

from e2_1_pn_codec import PNCodec
from e2_1_pn_codec_matrix import TEXTS
from verify_e0_snapshots import verify
from verify_e1_gate import EXPECTED_TRAJECTORY
from verify_e2_gate import completed_run, controls, require, resources


RUNS = ['baseline-final', 'derivation-final-1', 'derivation-final-2']
SOURCES = ['e2_1_pn_derive.c', 'e2_1_pn_derive.h', 'e2_1_pn_derive.py',
    'e2_1_pn_derive_probe.js', 'e2_1_pn_derive_matrix.py', 'E21PreferenceObserver.java',
    'e2_1_return_observer.js', 'run_e2_observed.py', 'e2_1_pn_codec.c', 'e2_1_pn_codec.h',
    'e2_1_pn_codec.py', 'e2_1_pn_codec_matrix.py', 'verify_e2_1j_pn_derive.py',
    'verify_e0_snapshots.py', 'verify_e1_gate.py', 'verify_e2_gate.py',
    'e0_dependency_observer.js', 'e2_jni_config_observer.js',
    'e1_resource_redirect.js', 'e1_fmod_binding.js']


def derivation_profile(source, material):
    require(source['source'] == 'original-app-context Settings.Secure', 'identity source unobserved')
    identity, package = source['identity'].encode('ascii'), source['package'].encode('ascii')
    require(re.fullmatch(b'[0-9a-f]{16}', identity) is not None
            and re.fullmatch(b'[A-Za-z0-9_.]{1,255}', package) is not None, 'source contract differs')
    table, iv = material['table'], bytes.fromhex(material['iv_source'])
    require(len(table) == 24 and all(type(value) is int and -(2 ** 31) <= value < 2 ** 31 for value in table)
            and 16 <= len(iv) <= 4095, 'private derivation material differs')
    transformed = bytes(((package[index] ^ table[index % 24]) & 31) + 48
                        for index in reversed(range(len(package))))
    return dict(keys=[hashlib.sha256(identity).hexdigest(), hashlib.sha256(transformed).hexdigest()],
                iv=iv[:16].hex())


def validate_matrix(matrix, native, material, config, codec, xml_before, xml_after):
    require(matrix['schema'] == 'e2.1j-derivation-matrix.v1'
            and native == dict(schema='e2.1j-native-derivation.v1', calls=6,
                initialization_replaced=False, original_xml_writer_replaced=False, no_vm_verified=False),
            'wrong bounded derivation schema or native call budget')
    require(all(matrix[key] is False for key in
                ['initialization_replaced', 'original_xml_writer_replaced', 'no_vm_verified'])
            and matrix['source_acquisition'] == 'Java retained; explicit transfer to native after complete trajectory',
            'unsupported derivation promotion')
    require(matrix['source']['package'] == config['package'], 'application package differs')
    expected = derivation_profile(matrix['source'], material)
    require(matrix['profile'] == expected and matrix['guest'] == dict(status=0,
                keys=list(b''.join(bytes.fromhex(key) for key in expected['keys'])),
                iv=list(bytes.fromhex(expected['iv']))), 'host/guest/hashlib derivation differs')
    info = matrix['cipher_info']
    require(len(info) == 2, 'original Cipher store missing')
    for index, row in enumerate(info):
        require(row['store'] == index and row['charset'].upper() == 'UTF-8'
                and all(base64.b64decode(row[key]['iv'], validate=True) == bytes.fromhex(expected['iv'])
                        for key in ['a', 'b']), 'actual original Cipher IV differs')
    require(matrix['restored'] is True and matrix['restore_receipt'] == 'pn-restored\t2'
            and matrix['cleanup_errors'] == [], 'PN or codec cleanup incomplete')
    require(xml_before == xml_after and len(xml_before) == 2
            and matrix['xml_before'] == matrix['xml_after'] == [
                dict(sha256=hashlib.sha256(raw).hexdigest(), bytes_hex=raw.hex()) for raw in xml_before],
            'original XML bytes changed or receipt differs')
    specifications = [(store, ordinal, purpose, text) for store in range(2)
                      for ordinal, text in enumerate(TEXTS) for purpose in ['key', 'value']]
    require(len(matrix['cases']) == len(specifications) == 40, 'actual Cipher matrix incomplete')
    for row, (store, ordinal, purpose, text) in zip(matrix['cases'], specifications):
        require((row['store'], row['ordinal'], row['purpose']) == (store, ordinal, purpose),
                'actual Cipher matrix order or coverage differs')
        expected_text = codec.encode(text, bytes.fromhex(expected['keys'][store]),
                                     bytes.fromhex(expected['iv']) if purpose == 'value' else None)
        require(row['original'] == row['native'] == expected_text, 'actual Cipher output differs')
    negatives = matrix['negatives']
    require([row['case'] for row in negatives] == ['empty-identity', 'non-hex-identity', 'short-table',
                'short-iv', 'invalid-package'] and all(row['result'] == dict(status=-1,
                keys=[165] * 64, iv=[165] * 16) for row in negatives), 'guest fail-closed matrix differs')
    return dict(original_cipher_cases=40, native_negative_cases=5, host_guest_hashlib_equal=True,
                original_xml_bytes_unchanged=True, stores=2)


def evaluate(manifest, base):
    require(manifest['schema'] == 'e2.1j-private-manifest.v1' and manifest['runs'] == RUNS,
            'wrong derivation manifest or run set')
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
    require(set(artifacts) == {'derive', 'dex', 'host', 'codec'}, 'build artifact set differs')
    for record in artifacts.values():
        require(hashlib.sha256(raw(record['path'])).hexdigest() == record['sha256'], 'build artifact differs')
    reproduction = load('build-reproduction.json')
    require(reproduction['status'] == 'matched' and len(reproduction['commands']) == 5
            and all(row['exit_code'] == 0 for row in reproduction['commands'])
            and reproduction['identities'] == {name: row['sha256'] for name, row in artifacts.items()},
            'derive/codec/observer build not reproduced')
    for name in ['e2_1_pn_derive.c', 'e2_1_pn_derive.h', 'e2_1_pn_codec.c',
                 'e2_1_pn_codec.h', 'E21PreferenceObserver.java']:
        require(reproduction['sources'][name] == manifest['files'][manifest['verification_sources'][name]],
                'reproduced source differs')
    after = load('guest-artifacts-after-final.json')
    require(after['exit_code'] == 0 and after['identities'] == {row['guest']: row['sha256']
            for row in artifacts.values() if 'guest' in row}, 'final guest artifact differs')
    material, origin = load('private-material.json'), load('material-origin.json')
    dex, dump = raw('private-source/installed-primary.dex'), raw('private-source/installed-primary.dexdump.txt').decode()
    require(hashlib.sha256(dex).hexdigest() == origin['dex_sha256']
            == 'ef1ac708f6e836daa925afc50d3bf3b139213b3a72323a354cc2e9493b50f608'
            and origin['material_sha256'] == manifest['files']['private-material.json'], 'private source identity differs')
    require(struct.unpack_from('<HHI', dex, origin['table_header_offset']) == (0x300, 4, 24)
            and origin['table_offset'] == origin['table_header_offset'] + 8
            and material['table'] == list(struct.unpack_from('<24i', dex, origin['table_offset'])),
            'signed derivation table differs from matched DEX')
    iv = re.search(r'const-string.*?, "(.*?)"', dump.splitlines()[origin['iv_dump_line']])[1].encode()
    require(iv in dex and len(iv) == 35 and material['iv_source'] == iv.hex(), 'IV source differs from matched DEX')
    baseline = RUNS[0]
    original, results, processes = load(baseline + '/identity-before.json'), [], set()
    codec = None
    try:
        for run in RUNS:
            prefix = run + '/'
            config = load(prefix + 'private-config.json')
            require(config['mode'] == 'original' and not any(key in config for key in
                    ['replacement', 'pn_codec', 'pn_bridge', 'pn_bridge_matrix', 'pn_matrix', 'java_oracle']),
                    'unrelated replacement/probe enabled')
            require(config == load(run + '.config.json') and load(prefix + 'run-start.json')['config_sha256']
                    == manifest['files'][run + '.config.json'], 'run configuration identity differs')
            completed_run(base, run, load(prefix + 'run-finished.json'),
                [json.loads(row) for row in raw(prefix + 'messages.jsonl').splitlines() if row.strip()])
            owned = load(prefix + 'owned-process.json')
            require(owned['pid'] not in processes and owned['stat'].startswith(str(owned['pid']) + ' '),
                    'independent process identity differs')
            processes.add(owned['pid'])
            identity = load(prefix + 'identity-before.json')
            require(identity == load(prefix + 'identity-after.json') and identity['files'] == original['files']
                    == config['guest_identity'] and identity['update_manifest_sha256']
                    == original['update_manifest_sha256'] == config['update_identity_sha256'], 'frozen inputs differ')
            for record in artifacts.values():
                if 'guest' in record:
                    require(identity['files'][record['guest']] == record['sha256'], 'actual derive/dex differs')
            commands = [json.loads(row) for row in raw(prefix + 'commands.jsonl').splitlines() if row.strip()]
            updates = [row for row in commands if row['argv'][-1].startswith('find ')
                       and 'update -type f -exec sha256sum' in row['argv'][-1]]
            require(len(updates) == 2 and all(row['code'] == 0 and len(row['stdout'].splitlines()) == 990
                    and hashlib.sha256(row['stdout'].strip().encode()).hexdigest() == identity['update_manifest_sha256']
                    for row in updates), '990-file before/after identity missing')
            require(raw(prefix + 'sources/run_e2_observed.py') == raw(manifest['verification_sources']['run_e2_observed.py'])
                    and load(prefix + 'run-start.json')['runner_sha256'] == manifest['files'][prefix + 'sources/run_e2_observed.py'],
                    'executed runner differs')
            for name in SOURCES:
                if prefix + 'sources/' + name in manifest['files']:
                    require(raw(prefix + 'sources/' + name) == raw(manifest['verification_sources'][name]),
                            'executed source differs: ' + name)
            for tick in range(3602):
                raw(prefix + f'snapshots/tick-{tick:06d}.bin')
            comparison = verify(base / baseline / 'snapshots', base / run / 'snapshots',
                                load(baseline + '/trace.json'), load(prefix + 'trace.json'))
            require(all(comparison[key] == value for key, value in EXPECTED_TRAJECTORY.items()), 'native trajectory differs')
            require(controls(load(prefix + 'control-receipts.json')) == controls(load(baseline + '/control-receipts.json')),
                    'actions/snapshot/restore/reset/terminal differ')
            resources(load(prefix + 'e0-final.json'), load(prefix + 'resource-final.json'), load(prefix + 'fmod-final.json'))
            cleanup = load(prefix + 'cleanup.json')
            require(cleanup['action'] == 'force-stop completed owned attempt' and cleanup['pid'] == owned['pid']
                    and cleanup['stat'].split(') ', 1)[1].split()[19] == owned['stat'].split(') ', 1)[1].split()[19],
                    'owned process cleanup identity differs')
            result = dict(run=run, **EXPECTED_TRAJECTORY, platform_asset_events=0, provider_failures=0, live_assets=0)
            if run == baseline:
                require('pn_derivation' not in config and 'return_observer' not in config, 'baseline observer enabled')
            else:
                settings = config['return_observer']
                require(settings['install_at'] == 'post-cold-ready' and settings['release_after_uninstall'] is True
                        and settings['enable_pn_matrix'] is True and settings['enable_pn_codec'] is True
                        and settings['enable_pn_derivation'] is True and settings['dex_path'] == artifacts['dex']['guest'],
                        'derivation observer configuration differs')
                require(load(prefix + 'return-observer-uninstall.json') == {'result': 'uninstalled\t2'}
                        and load(prefix + 'return-observer-release.json') == {'result': 'released\t6'},
                        'original Java observer references retained')
                configuration = config['pn_derivation']
                require(configuration['library'] == artifacts['derive']['guest']
                        and configuration['crypto_library'] == '/system/lib64/libcrypto.so'
                        and Path(configuration['material']).resolve() == base / 'private-material.json'
                        and Path(configuration['host_library']).resolve() == base / artifacts['host']['path']
                        and Path(configuration['host_codec_library']).resolve() == base / artifacts['codec']['path'],
                        'actual derivation libraries or material differ')
                if codec is None:
                    codec = PNCodec(configuration['host_codec_library'], configuration['host_crypto_library'])
                matrix = load(prefix + 'pn-derivation-matrix.json')
                require(matrix['material_sha256'] == manifest['files']['private-material.json'], 'matrix material differs')
                result['derivation'] = validate_matrix(matrix, load(prefix + 'pn-derivation-final.json'),
                    material, configuration, codec,
                    [raw(prefix + f'derivation-xml-before/store-{index}.xml') for index in range(2)],
                    [raw(prefix + f'derivation-xml-after/store-{index}.xml') for index in range(2)])
            results.append(result)
    finally:
        if codec is not None:
            codec.close()
    return dict(schema='e2.1j-bounded-summary.v1', status='bounded_evidence_verified', runs=results,
        attachment_count=len(manifest['files']), strict_gate='BLOCKED', removed_keys=0,
        initialization_replaced=False, original_xml_writer_replaced=False, no_vm_verified=False,
        android_free_verified=False, training_admissible=False, tenfold_speedup_verified=False,
        unresolved=['original initialization integration and identity source without Java',
            'native XML persistence, backup delivery, concurrency and crash durability', '46-key removability'])


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
