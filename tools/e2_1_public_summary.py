"""Export bounded E2.1 findings without raw values, addresses or attachment paths."""

import argparse
import hashlib
import json
from pathlib import Path


def digest(value):
    raw = value.encode('utf-8')
    return dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def summaries(manifest, base, gate):
    if gate.get('bounded_native_comparisons') != 'passed' or gate.get('status') != 'blocked':
        raise ValueError('expected identity-verified bounded evidence with blocked strict gate')
    if gate['attachment_sha256'] != manifest['files']:
        raise ValueError('gate attachment identity differs')

    def load(relative):
        raw = (base / relative).read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest['files'][relative]:
            raise ValueError('attachment changed')
        return json.loads(raw)

    public_gate = {key: value for key, value in gate.items() if key != 'attachment_sha256'}
    public_gate['private_manifest_identity'] = digest(json.dumps(manifest, sort_keys=True))
    matrix = []
    for run in manifest['runs']:
        config = load(run + '/private-config.json')
        if not config.get('semantics_probe'):
            continue
        probe = load(run + '/semantics-final.json')
        for row in probe['results']:
            item = dict(run=run, case=row['case'], operation=row['operation'], entry=row.get('entry', 'unknown'),
                        entry_offset=row.get('entry_offset', 'unknown'), thread=row['thread'],
                        exception_state=row['exception_state'], error_status=row['error_status'],
                        pn_native_replacement=False)
            if 'input' in row:
                item['input_summary'] = digest(row['input'])
            if row['operation'] == 'read':
                item['output_summary'] = digest(row['result']['value'])
            if row['operation'] == 'exists':
                item['boolean_result'] = row['result']['boolean']
            matrix.append(item)
    semantic = dict(schema='e2.1-public-semantics.v1', rows=matrix, values_redacted=True,
                    pn_original_cold_chain=gate['pn_original_cold_chain'],
                    original_bridge_scope='probe-induced raw/top calls; PN remains original JNI fallback',
                    string_lifecycle=load(manifest['string_lifecycle']),
                    unknown=['commit/apply timing', 'concurrent read/write', 'repeated configuration constructor',
                             'Java reference retirement', 'configuration store close/free',
                             'exception-triggered cleanup', 'arrays/objects/MUTF-8',
                             'true maximum value length and overflow; 4095 is probe bound'],
                    no_vm_verified=False, training_admissible=False)
    selected = {'FileSystemBootstrap', 'ProviderBootstrap', 'APKProviderRegister', 'ReaderRegister',
                'SCDBOpen', 'GameConfigurationInit', 'GamePreinit', 'GamePreinitConfigure',
                'ContentBootstrap', 'TableLoadBatch', 'StateMaskStage', 'IDRegistration',
                'GameStateConstructor'}
    observer = load('matrix-6-strict-lifecycle/e0-final.json')
    entries = [{key: row[key] for key in ['target', 'labels', 'phase', 'caller', 'thread', 'count',
                                        'first_sequence', 'native_scopes', 'attribution']}
               for row in observer['counts'] if selected.intersection(row['labels'])]
    comparison = dict(schema='e2.1-public-comparison.v1', runs=gate['runs'],
                      initializer_observations=entries,
                      initializer_contract='entry/order/caller observed; size/alignment/ownership/failure cleanup unknown',
                      hook_gaps=[dict(label=row['label'], target=row['target'], count=None, status='uncovered')
                                 for row in observer['errors']],
                      private_attachment_count=gate['attachment_count'],
                      private_manifest_identity=public_gate['private_manifest_identity'],
                      evidence_scope='fresh Android/JavaVM processes, fixed native trajectory only',
                      no_vm_verified=False, android_free_verified=False, training_admissible=False)
    return {'e2_1_gate.json': public_gate, 'e2_1_semantics_matrix.json': semantic,
            'e2_1_comparison_manifest.json': comparison}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--gate', required=True, type=Path)
    parser.add_argument('--output-directory', required=True, type=Path)
    args = parser.parse_args()
    result = summaries(json.loads(args.manifest.read_text()), args.manifest.parent,
                       json.loads(args.gate.read_text()))
    for name, data in result.items():
        with (args.output_directory / name).open('x') as output:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write('\n')
    print(json.dumps(dict(status='exported', files=list(result), strict_gate='blocked')))


if __name__ == '__main__':
    main()
