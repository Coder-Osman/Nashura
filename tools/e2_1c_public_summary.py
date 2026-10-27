"""Export only allowlisted counters from verified private PN return/failure evidence."""

import argparse
import hashlib
import json
from pathlib import Path

from e2_1b_public_summary import digest, label, number
from verify_e2_1c_evidence import evaluate


def commits(values):
    return {label(key, '[op]:(true|false)'): number(value) for key, value in values.items()}


def cases(rows):
    return [dict(case=label(row['case'], '[a-z_]+'), operation=label(row['operation'], '(read|write)'),
                 commits=commits(row['commits'])) for row in rows]


def summarize(evidence):
    if evidence['status'] != 'bounded_evidence_verified' or evidence['strict_gate'] != 'BLOCKED':
        raise ValueError('bounded evidence required; strict gate is separate')
    for field in ['no_vm_verified', 'android_free_verified', 'training_admissible',
                  'tenfold_speedup_verified', 'pn_native_replacement']:
        if evidence[field] is not False:
            raise ValueError('unsupported gate promotion')
    if evidence['removed_keys'] != 0 or evidence['art_coverage']['exhaustive'] is not False:
        raise ValueError('unsupported removal or exhaustive coverage claim')
    runs = []
    for row in evidence['runs']:
        if type(row['observed']) is not bool:
            raise ValueError('invalid observer flag')
        runs.append(dict(run=label(row['run'], '(baseline|observed|observed-v2|diagnostic|diagnostic-[23])'),
                         forwarding_observer=row['observed'],
                         **{field: number(row[field]) for field in [
                             'snapshot_count', 'terminal_tick', 'injected_actions', 'total_bytes']},
                         trajectory_sha256=digest(row['trajectory_sha256']),
                         platform_asset_events=0, provider_failures=0, live_assets=0))
    race = evidence['race']
    coverage = evidence['art_coverage']
    return dict(schema='e2.1c-public-evidence.v1', status='bounded_evidence_verified', strict_gate='BLOCKED',
                private_attachment_count=number(evidence['attachment_count']), runs=runs,
                sequential=cases(evidence['sequential']),
                failure=dict(cases=cases(evidence['failure']['cases']),
                             delegated_commit_returns='false in both original stores',
                             disk_bytes='unchanged in fault window', memory_read='updated in same process',
                             native_write_error='void; no pending JNI exception observed',
                             durability='not established'),
                race=dict(**{field: number(race[field]) for field in [
                    'workers', 'writes', 'reads', 'overlapping_art_root_pairs',
                    'own_reads', 'other_reads', 'unknown_reads']}, commits=commits(race['commits']),
                    general_thread_safety='unknown'),
                art_coverage=dict(exhaustive=False, unnamed_semantics='unknown',
                    **{field: number(coverage[field]) for field in [
                        'failure_unnamed_methods', 'race_unnamed_methods',
                        'failure_unnamed_records', 'race_unnamed_records']},
                    original_strict_parser='unchanged; rejects unnamed IDs'),
                source_rebuild='DEX and native helper byte-identical; API 30',
                scope='post-cold-ready PN_POPUP forwarding observation; original Java stores retained',
                evidence_access='private attachments required; public summary is not native revalidation',
                no_vm_verified=False, android_free_verified=False, training_admissible=False,
                tenfold_speedup_verified=False, pn_native_replacement=False, removed_keys=0,
                retained_failed_attempts=number(evidence['retained_failed_attempts']),
                unresolved=['46-key removability', 'natural cold-start commit return values',
                            'native replacement persistence and failure equivalence',
                            'general concurrency and crash durability', 'Java reference retirement'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    evidence = evaluate(json.loads(args.manifest.read_text()), args.manifest.resolve().parent)
    summary = summarize(evidence)
    summary['private_manifest_sha256'] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    with args.output.open('x') as output:
        json.dump(summary, output, indent=2)
        output.write('\n')
    print(json.dumps(dict(status=summary['status'], strict_gate=summary['strict_gate'])))


if __name__ == '__main__':
    main()
