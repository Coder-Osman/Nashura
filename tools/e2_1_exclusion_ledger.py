"""Derive exclusions from private observations; publish value digests only."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def value_digest(value):
    if not value or value.get('status') != 'decoded':
        return dict(status=(value or {}).get('status', 'unknown'))
    raw = value['value'].encode('utf-8', errors='surrogatepass')
    return dict(status='empty' if not raw else 'decoded', bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest())


def build(original, replacement, contract, identities, threads=None):
    excluded = contract['excluded_keys']
    events = replacement['events']
    fallback = [row for row in events if row.get('route') == 'original_fallback']
    if len(fallback) != replacement['status']['fallback']:
        raise ValueError('fallback counter disagrees with events')
    if any(row.get('key') not in excluded for row in fallback):
        raise ValueError('fallback absent from exclusion contract')
    if original['dropped']['config_calls'] or original['errors']:
        raise ValueError('incomplete original evidence')
    by_sequence = {row['sequence']: row for row in original['config_calls']}
    ledger = []
    for key, declaration in excluded.items():
        source = [row for row in original['config_calls'] if row['key'].get('value') == key]
        if not source or not declaration.get('evidence') or any(
                sequence not in by_sequence or by_sequence[sequence]['key'].get('value') != key
                for sequence in declaration['evidence']):
            raise ValueError('exclusion source mismatch: ' + key)
        actual = [row for row in fallback if row['key'] == key]
        observed = []
        for row in source:
            observed.append(dict(sequence=row['sequence'], operation=row['operation'],
                                 phase=row['phase'], thread=row['thread'], caller=row['caller'],
                                 value=value_digest(row.get('value') if 'Write' in row['operation']
                                                    else row.get('returned_string')),
                                 boolean_result=row.get('boolean_result', 'unknown')))
        native_rows = [row for row in source if row['operation'] in
                       {'PreferenceNativeReadBridge', 'PreferenceNativeWriteBridge'}]
        values = [item['value'] for item in observed if item['value']['status'] in {'decoded', 'empty'}]
        ledger.append(dict(key=key, operations=sorted({row['operation'] for row in source}),
                           value_type='string' if values else 'unknown',
                           value_type_scope='native bridge output; Java storage type unknown',
                           observed_values=observed,
                           original_return_status=sorted({item['status'] for item in values}) or ['unknown'],
                           first_phase=source[0]['phase'], last_phase=source[-1]['phase'],
                           threads=sorted({row['thread'] for row in source}),
                           callers=sorted({row['caller'] for row in source}),
                           jni_operations='unknown; no per-call JNI stack correlation in historical evidence',
                           fallback_count=len(actual),
                           side_effect='counter' if key == 'PN_POPUP' else 'unknown',
                           stateful=True if any('Write' in row['operation'] for row in source) else 'unknown',
                           replacement_class='required',
                           classification_basis='reachable in unchanged initialization; removal has no proof',
                           evidence=[row['sequence'] for row in native_rows],
                           reason_if_excluded=declaration['reason']))
    fallback_events = []
    for index, row in enumerate(fallback):
        fallback_events.append(dict(index=index, key=row['key'], operation=row['operation'],
                                    caller=row.get('caller', 'unknown'), thread=row.get('thread', 'unknown'),
                                    thread_name=(threads or {}).get(row.get('thread'), 'unknown'),
                                    native_scope=row.get('native_scope'), phase=row.get('phase', 'unknown'),
                                    stack_summary=row.get('backtrace', 'unknown'),
                                    input_summary=value_digest(dict(status='decoded', value=row['key'])),
                                    return_summary='unknown; historical fallback dispatch returns before original',
                                    error_status=row.get('error', 'unknown'),
                                    source_correlation='key-level only; different process, no inferred value/sequence join'))
    return dict(schema='e2.1-exclusion-ledger.v1', source_sha256=identities,
                unique_excluded_keys=len(ledger), fallback_events_count=len(fallback),
                classification_counts=dict(Counter(row['replacement_class'] for row in ledger)),
                rows=ledger, fallback_events=fallback_events,
                values_redacted=True, no_vm_verified=False, training_admissible=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', required=True, type=Path)
    parser.add_argument('--replacement', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--threads', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    paths = dict(original=args.original, replacement=args.replacement, config=args.config)
    thread_names = {}
    if args.threads:
        paths['threads'] = args.threads
        thread_names = {row['tid']: row['comm'] for row in json.loads(args.threads.read_text())['threads']}
    result = build(json.loads(args.original.read_text()), json.loads(args.replacement.read_text()),
                   json.loads(args.config.read_text())['replacement'],
                   {name: sha(path) for name, path in paths.items()}, thread_names)
    with args.output.open('x') as target:
        json.dump(result, target, indent=2)
        target.write('\n')
    print(json.dumps({key: result[key] for key in ['unique_excluded_keys', 'fallback_events_count', 'classification_counts']}))


if __name__ == '__main__':
    main()
