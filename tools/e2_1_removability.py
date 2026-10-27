"""Separate containment policy from necessity using bounded real observations."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


BRIDGES = {'PreferenceNativeReadBridge': 'read', 'PreferenceNativeWriteBridge': 'write'}
WRAPPERS = {'PreferenceRead', 'PreferenceWrite', 'PreferenceExists', 'PreferenceRemove'}


def build(ledger, observer, probes, identities):
    keys = [row['key'] for row in ledger['rows']]
    if len(keys) != len(set(keys)) or ledger['unique_excluded_keys'] != len(keys):
        raise ValueError('duplicate or inconsistent historical exclusions')
    if observer['errors'] or observer['dropped']['config_calls']:
        raise ValueError('incomplete native configuration evidence')
    events = [row for row in observer['config_calls']
              if row['operation'] in BRIDGES and row['key'].get('value') in keys]
    expected_probes = Counter((row['key'], row['thread'], row['operation']) for row in probes)
    actual_probes = Counter((row['key']['value'], row['thread'], BRIDGES[row['operation']])
                            for row in events if (row['key']['value'], row['thread'],
                                                  BRIDGES[row['operation']]) in expected_probes)
    if actual_probes != expected_probes:
        raise ValueError('explicit probe count/thread attribution is ambiguous')
    natural = [row for row in events if (row['key']['value'], row['thread'],
                                        BRIDGES[row['operation']]) not in expected_probes]
    rows = []
    for previous in ledger['rows']:
        source = [row for row in natural if row['key']['value'] == previous['key']]
        wrappers = [row for row in observer['config_calls']
                    if row['operation'] in WRAPPERS and row['key'].get('value') == previous['key']]
        wrapper_observations = [dict(operation=row['operation'], caller=row['caller'],
            thread=row['thread'], phase=row['phase'], phase_chain=row.get('phase_chain', []),
            sequence=row['sequence']) for row in wrappers]
        rows.append(dict(key=previous['key'], containment_policy='retain original route',
            historical_replacement_class=previous['replacement_class'],
            battle_necessity='unknown', removability='unknown',
            original_initialization_reachable=bool(source),
            natural_bridge_calls=len(source), operations=dict(Counter(row['operation'] for row in source)),
            phases=sorted({row['phase'] for row in source}),
            callers=sorted({row['caller'] for row in source}),
            threads=sorted({row['thread'] for row in source}),
            source_sequences=[row['sequence'] for row in source],
            wrapper_observations=wrapper_observations,
            wrapper_counting='separate alias layer; not added to bridge calls; consumer semantics unknown',
            removal_intervention='not performed',
            state_effect='unknown; unchanged route observation is not a removal experiment',
            proof_required='identify consumer and ownership; test a justified counterfactual with unchanged initialization'))
    return dict(schema='e2.1b-removability.v1', source_sha256=identities, excluded_keys=len(rows),
        natural_bridge_calls=len(natural), explicit_probe_calls=sum(actual_probes.values()),
        classification_counts=dict(Counter(row['removability'] for row in rows)), rows=rows,
        inference_limit='finite fixed trajectory only; zero observed calls does not prove removability',
        counted_layer='native read/write bridges only; JNI and wrapper aliases not added',
        no_vm_verified=False, android_free_verified=False, training_admissible=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--observer', type=Path, required=True)
    parser.add_argument('--probes', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    paths = [args.ledger, args.observer, *args.probes]
    identities = {str(index): hashlib.sha256(path.read_bytes()).hexdigest() for index, path in enumerate(paths)}
    probes = [row for path in args.probes for row in json.loads(path.read_text())]
    result = build(json.loads(args.ledger.read_text()), json.loads(args.observer.read_text()), probes, identities)
    with args.output.open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps({key: result[key] for key in ['excluded_keys', 'natural_bridge_calls', 'explicit_probe_calls',
                                                'classification_counts']}))


if __name__ == '__main__':
    main()
