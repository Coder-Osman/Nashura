"""Publish a value-free allowlisted summary after verifying private E2.1-B evidence."""

import argparse
import hashlib
import json
from pathlib import Path
import re

from verify_e2_1b_evidence import evaluate


def number(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid public counter')
    return value


def digest(value):
    if not isinstance(value, str) or re.fullmatch('[a-f0-9]{64}', value) is None:
        raise ValueError('invalid public digest')
    return value


def label(value, pattern):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError('invalid public static label')
    return value


def summarize(evidence):
    if evidence['status'] != 'bounded_evidence_verified' or evidence['strict_gate'] != 'BLOCKED':
        raise ValueError('bounded verification required; strict gate is separate')
    if any(evidence[field] is not False for field in [
            'no_vm_verified', 'android_free_verified', 'training_admissible', 'pn_native_replacement']):
        raise ValueError('unsupported gate promotion')
    rows = []
    for row in evidence['removability']['rows']:
        if row['removability'] != 'unknown' or row['battle_necessity'] != 'unknown':
            raise ValueError('no removal experiment verified by this exporter')
        wrappers = [dict(
            operation=label(item['operation'], 'Preference(Read|Write|Exists|Remove)'),
            caller=label(item['caller'], r'[A-Za-z0-9_.-]+\+0x[a-f0-9]+'),
            thread=number(item['thread']), sequence=number(item['sequence']),
            phase=label(item['phase'], '[A-Za-z0-9_-]+'),
            phase_chain=[label(phase, '[A-Za-z0-9_-]+') for phase in item['phase_chain']])
            for item in row['wrapper_observations']]
        rows.append(dict(key=label(row['key'], '[A-Za-z0-9_.-]+'),
            containment_policy='retain original route', historical_replacement_class='required',
            battle_necessity='unknown', removability='unknown', removal_intervention='not performed',
            natural_bridge_calls=number(row['natural_bridge_calls']),
            operations={label(key, 'PreferenceNative(Read|Write)Bridge'): number(value)
                        for key, value in row['operations'].items()},
            callers=[label(value, r'[A-Za-z0-9_.-]+\+0x[a-f0-9]+') for value in row['callers']],
            phases=[label(value, '[A-Za-z0-9_-]+') for value in row['phases']],
            threads=[number(value) for value in row['threads']],
            source_sequences=[number(value) for value in row['source_sequences']],
            wrapper_observations=wrappers,
            interpretation='host phase and callsite observations; consumer semantics and removal effects unknown'))
    if len(rows) != 46 or len({row['key'] for row in rows}) != 46:
        raise ValueError('46 distinct exclusion labels required')
    runs = []
    for row in evidence['runs']:
        summary = dict(run=label(row['run'], '(baseline|series-[1-4]-(original|shadow|replacement|strict-targeted))'),
            mode=label(row['mode'], '(original|shadow|replacement|strict-targeted)'),
            comparison={field: number(row['comparison'][field]) for field in [
                'snapshot_count', 'terminal_tick', 'injected_actions', 'total_bytes']},
            trajectory_sha256=digest(row['comparison']['trajectory_sha256']),
            resource_platform_events=number(row['resource_platform_events']),
            provider_failures=number(row['provider_failures']), live_assets=number(row['live_assets']),
            jni_samples_dropped=number(row['jni_samples_dropped']), exhaustive_jni_arguments=False)
        for field in ['fallback_calls_including_probes', 'targeted_reads', 'targeted_writes']:
            if field in row:
                summary[field] = number(row[field])
        if 'pn_sequence' in row:
            sequence = row['pn_sequence']
            summary['pn_sequence'] = dict(trace_sha256=digest(sequence['trace_sha256']),
                records=number(sequence['records']),
                cases=[dict(case=label(item['case'], '[a-z_]+'),
                            operation=label(item['operation'], '(read|write)'),
                            commits=number(item['commits']), completion='returned') for item in sequence['cases']],
                commit_return_value='unknown', power_loss_durability='unknown')
        if 'cross_process_prepared_read_and_increment' in row:
            summary['fresh_process_prepared_read_and_increment'] = 'verified'
        runs.append(summary)
    concurrency = evidence['concurrent']
    result = dict(schema='e2.1b-public-evidence.v1', status='bounded_evidence_verified',
        strict_gate='BLOCKED', no_vm_verified=False, android_free_verified=False,
        training_admissible=False, tenfold_speedup_verified=False,
        pn_native_replacement=False, removed_keys=0, runs=runs,
        source_apk_sha256=digest(evidence['source_apk_sha256']),
        installed_dex_sha256=digest(evidence['installed_dex_sha256']),
        concurrent={field: number(concurrency[field]) for field in [
            'workers', 'write_calls', 'read_calls', 'commit_calls', 'overlapping_java_root_pairs']},
        concurrent_trace_sha256=digest(concurrency['trace_sha256']),
        concurrency_scope='same saved value only; different-value races and general thread safety unknown',
        trace_scope='explicit post-cold-ready probe windows; not natural cold-start or complete-battle Java traces',
        evidence_scope='private attachments required for native revalidation; public summary is not raw evidence')
    ledger = dict(schema='e2.1b-public-removability.v1', excluded_keys=46,
        natural_bridge_calls=number(evidence['removability']['natural_bridge_calls']),
        explicit_probe_calls=number(evidence['removability']['explicit_probe_calls']),
        classification_counts=dict(unknown=46), rows=rows,
        counting='native bridge layer only; wrapper and JNI aliases are nonadditive',
        inference_limit='finite fixed trajectory; observed reachability is not necessity or removability',
        no_vm_verified=False, training_admissible=False)
    return result, ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    evidence = evaluate(manifest, args.manifest.resolve().parent)
    summary, ledger = summarize(evidence)
    summary['private_manifest_sha256'] = digest(hashlib.sha256(args.manifest.read_bytes()).hexdigest())
    summary['private_attachment_count'] = number(len(manifest['files']))
    outputs = dict(e2_1b_evidence_summary=summary, e2_1b_removability_ledger=ledger)
    if any((args.output_directory / (name + '.json')).exists() for name in outputs):
        raise FileExistsError('public summary already exists; select a new output directory')
    for name, result in outputs.items():
        with (args.output_directory / (name + '.json')).open('x') as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write('\n')
    print(json.dumps(dict(status=summary['status'], strict_gate='BLOCKED', public_files=len(outputs))))


if __name__ == '__main__':
    main()
