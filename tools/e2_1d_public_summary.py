"""Allowlist publication fields; private preference values and paths stay private."""

import argparse
import hashlib
import json
from pathlib import Path

from e2_1b_public_summary import digest, number
from verify_e2_1d_evidence import evaluate


def summarize(evidence):
    if evidence['status'] != 'bounded_evidence_verified' or evidence['strict_gate'] != 'BLOCKED':
        raise ValueError('bounded evidence required; strict gate remains separate')
    for field in ['no_vm_verified', 'android_free_verified', 'training_admissible',
                  'tenfold_speedup_verified', 'pn_native_replacement']:
        if evidence[field] is not False:
            raise ValueError('unsupported gate promotion')
    if evidence['removed_keys'] != 0 or evidence['restart_readback_equal'] is not True:
        raise ValueError('unsupported removal or restart claim')
    runs = []
    for row in evidence['runs']:
        if row['run'] not in {'baseline', 'cold-3', 'cold-4'}:
            raise ValueError('unexpected run label')
        result = dict(run=row['run'], **{field: number(row[field]) for field in [
            'snapshot_count', 'terminal_tick', 'injected_actions', 'total_bytes']},
            trajectory_sha256=digest(row['trajectory_sha256']),
            platform_asset_events=0, provider_failures=0, live_assets=0)
        if 'cold' in row:
            cold = row['cold']
            result['cold'] = {field: number(cold[field]) for field in [
                'native_entries', 'bridge_onleave_events', 'external_windows',
                'released_java_references', 'allocated_global_references', 'deleted_global_references']}
            result['cold']['natural_read_commits'] = 0
            result['cold']['natural_write_commits'] = {'o:true': 1, 'p:true': 1}
        runs.append(result)
    return dict(schema='e2.1d-public-evidence.v1', status='bounded_evidence_verified', strict_gate='BLOCKED',
                private_attachment_count=number(evidence['attachment_count']), runs=runs,
                restart_readback_equal=True, restart_scope='normal process restart; not crash or power-loss durability',
                return_scope='two original libg PN_POPUP callsites; independent observer thread/caller/phase correlation',
                return_address_limit='consistent relative callers and page-aligned module base; no independent module-base receipt',
                onleave_failure='early bridge onLeave not observed; cause unknown; external probes separated',
                retirement_scope='observer-owned explicit references only; no GC, class unload or game-object lifecycle proof',
                evidence_access='private attachments required; public summary is not native revalidation',
                retained_incomplete_runs=number(evidence['retained_incomplete_runs']), removed_keys=0,
                no_vm_verified=False, android_free_verified=False, training_admissible=False,
                tenfold_speedup_verified=False, pn_native_replacement=False,
                unresolved=['46-key removability', 'PN native persistence and failure equivalence',
                            'game Java object lifetime', 'general concurrency and crash durability'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(evaluate(json.loads(args.manifest.read_text()), args.manifest.resolve().parent))
    result['private_manifest_sha256'] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    with args.output.open('x') as target:
        json.dump(result, target, indent=2)
        target.write('\n')
    print(json.dumps(dict(status=result['status'], strict_gate=result['strict_gate'])))


if __name__ == '__main__':
    main()
