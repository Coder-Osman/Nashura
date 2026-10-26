"""Export allowlisted aggregates; never publish native values, pointers or audit rows."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from verify_e2_gate import evaluate


def read(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def aggregate(rows, field):
    result = Counter()
    for row in rows:
        result[row.get(field, 'unknown')] += row['count']
    return dict(sorted(result.items()))


def observation(directory):
    observed = read(directory / 'e2-final.json')
    names = defaultdict(set)
    for path in directory.glob('threads-*.json'):
        for row in read(path).get('threads', []):
            names[row['tid']].add(row['comm'])
    roles = {}
    for tid, labels in names.items():
        if len(labels) != 1:
            roles[tid] = 'unknown_renamed'
        elif 'Mainloop' in next(iter(labels)):
            roles[tid] = 'Mainloop_name_observed'
        elif 'FMOD' in next(iter(labels)).upper():
            roles[tid] = 'FMOD_name_observed'
        else:
            roles[tid] = 'other_named_thread'
    result = dict(source_sha256=digest(directory / 'e2-final.json'),
                  dropped=observed['dropped'], errors=len(observed['errors']),
                  config_rows=len(observed['config_calls']),
                  thread_name_observation=bool(names), reference_liveness='unknown')
    for source in ['jni_counts', 'java_vm_counts', 'reference_counts']:
        rows = observed[source]
        result[source] = dict(total=sum(row['count'] for row in rows),
                              by_phase=aggregate(rows, 'phase'),
                              by_operation=aggregate(rows, 'name' if source != 'reference_counts' else 'operation'))
        result[source]['by_thread_role'] = aggregate([dict(row, role=roles.get(row['thread'], 'unknown')) for row in rows], 'role')
        result[source]['by_caller_module'] = aggregate([dict(row, module=row.get('caller', 'unknown').split('+', 1)[0]) for row in rows], 'module')
        result[source]['same_thread_scope_events'] = sum(row['count'] for row in rows if row.get('phase_chain'))
    result['state_effect'] = 'whole fixed trajectory unchanged; effect of individual calls unknown'
    result['phase_caveat'] = 'empty phase chain means host-command overlap, not battle-thread causality'
    result['uncovered'] = list(observed['unknowns'])
    return result


def attempts(root):
    result = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not directory.name.startswith(('original-', 'shadow-', 'targeted-', 'negative-', 'no-vm-', 'final-', 'thread-audit-')):
            continue
        receipt = directory / 'run-finished.json'
        value = read(receipt) if receipt.exists() else {}
        entry = dict(attempt=directory.name, output_directory_token=directory.name,
                     recorded_status=value.get('status', 'unknown'), recorded_exit_code=value.get('exit_code'),
                     evidence_incomplete=(directory / 'evidence-incomplete.json').exists())
        if receipt.exists():
            entry['receipt_sha256'] = digest(receipt)
        native = directory / 'native-1.txt'
        if native.exists():
            entry['original_preinit_return_observed'] = 'original_preinit_returned' in native.read_text()
            entry['native_stdout_sha256'] = digest(native)
        result_path = directory / 'result.json'
        if result_path.exists():
            value = read(result_path)
            entry['result_status'] = value.get('status')
            entry['no_vm_verified'] = value.get('no_vm_verified', False)
            entry['result_sha256'] = digest(result_path)
        result.append(entry)
    return result


def generate(manifest_path, supplement):
    root = manifest_path.parent
    manifest = read(manifest_path)
    gate = evaluate(manifest, root)
    runs = [manifest['original']] + manifest['runs']
    observations = {name: observation(root / name) for name in runs + supplement}
    original = read(root / manifest['original'] / 'e2-final.json')
    contract = read(root / manifest['runs'][0] / 'private-config.json')['replacement']
    key_rows = []
    for index, (key, spec) in enumerate(sorted(contract['entries'].items()), 1):
        rows = [row for row in original['config_calls'] if row['key'].get('value') == key]
        key_rows.append(dict(id='target-%02d' % index, type='engine-string', writable=spec['writable'],
                             original_observed_calls=len(rows),
                             first_phase=min(rows, key=lambda row: row['sequence'])['phase'],
                             last_phase=max(rows, key=lambda row: row['sequence'])['phase'],
                             operations=dict(Counter(row['operation'] for row in rows)),
                             observed_value_kind='empty' if spec['value'] == '' else 'nonempty',
                             default_semantics='unknown beyond observed value'))
    public_gate = {key: value for key, value in gate.items() if key != 'attachment_sha256'}
    public_gate.update(private_manifest_sha256=digest(manifest_path),
                       attachment_count=len(gate['attachment_sha256']),
                       private_attachments_distributed=False,
                       strict_cli_exit_code=2 if gate['status'] == 'blocked' else 0)
    comparison = dict(schema='e2-public-comparisons.v1', original=manifest['original'],
                      required_runs=gate['runs'], supplemental_observers=supplement,
                      update_manifest_sha256=manifest['update_manifest_sha256'],
                      input_sha256=sorted(set(manifest['required_guest_identity'].values())),
                      private_manifest_sha256=digest(manifest_path),
                      snapshot_bytes_per_run=9120440, actions=28, ticks=3601, snapshots=3602,
                      normalization='only snapshot directory in control reply; snapshot bytes unchanged',
                      scope='fixed engineering trajectory, not arbitrary matches or training admission')
    comparison['supplemental_comparisons'] = {name: dict(
        receipt=read(root / name / 'supplemental-comparison.json'),
        receipt_sha256=digest(root / name / 'supplemental-comparison.json'),
        source_identity_sha256=digest(root / name / 'source-identity.json')) for name in supplement}
    return {'e2_gate.json': public_gate,
            'e2_comparison_manifest.json': comparison,
            'e2_dynamic_dependency_counts.json': dict(schema='e2-public-dependency-counts.v1',
                runs=observations, declared_keys=key_rows, declared_key_count=32,
                individually_excluded_key_count=len(contract['excluded_keys']),
                semantics='aggregates only; key/value/handle/raw audit remain private'),
            'e2_failure_index.json': dict(schema='e2-public-attempt-index.v1', attempts=attempts(root),
                missing_receipt_means='unknown, never success',
                failures_preserved=True)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--supplement', nargs='*', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    outputs = generate(args.manifest, args.supplement)
    args.output.mkdir(exist_ok=True, parents=True)
    for name, value in outputs.items():
        with (args.output / name).open('x') as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write('\n')
    print(json.dumps(dict(status='exported', files=list(outputs), strict_gate=outputs['e2_gate.json']['status'])))


if __name__ == '__main__':
    main()
