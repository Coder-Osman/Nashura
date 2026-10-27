"""Summarize real ART v3 method traces; retain no argument values or raw trace."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct


ROOT_CLASS = 'com.supercell.titan.ApplicationUtilBase'
ROOT_METHODS = {'getKeyValue', 'storeKeyValue'}


def selected(method):
    owner, name, _ = method
    return (owner == ROOT_CLASS and name in ROOT_METHODS
            or owner == 'c.a' and name in {'j', 'n'}
            or owner == 'com.supercell.titan.GameApp' and name == 'requestBackup'
            or owner == 'javax.crypto.Cipher' and name == 'doFinal'
            or owner.startswith('android.app.SharedPreferencesImpl')
            and name in {'commit', 'apply', 'commitToMemory', 'writeToFile', 'enqueueDiskWrite'})


def parse(raw):
    boundary = raw.find(b'*end\n')
    if boundary < 0:
        raise ValueError('trace text header missing')
    text = raw[:boundary].decode('utf-8')
    lines = text.splitlines()
    if lines[:2] != ['*version', '3'] or 'data-file-overflow=false' not in lines:
        raise ValueError('unsupported or overflowed trace')
    metadata = dict(line.split('=', 1) for line in lines if '=' in line and not line.startswith('0x'))
    methods = {}
    for line in lines[lines.index('*methods') + 1:]:
        fields = line.split('\t')
        if len(fields) < 4 or not (fields[0].startswith('0x') or fields[0] == '0'):
            raise ValueError('invalid trace method table')
        identifier = int(fields[0], 0)
        if identifier in methods or identifier & 3:
            raise ValueError('duplicate or unaligned trace method')
        methods[identifier] = tuple(fields[1:4])
    binary = raw[boundary + 5:]
    if len(binary) < 32:
        raise ValueError('short trace binary header')
    magic, version, offset, _, record_size = struct.unpack_from('<4sHHQH', binary)
    if (magic, version, offset, record_size, metadata.get('clock')) != (b'SLOW', 3, 32, 14, 'dual'):
        raise ValueError('unsupported trace binary layout')
    if (len(binary) - offset) % record_size:
        raise ValueError('partial trace record')
    record_count = (len(binary) - offset) // record_size
    if record_count != int(metadata['num-method-calls']):
        raise ValueError('method record count differs')
    counts = Counter()
    roots = []
    active = {}
    for sequence, position in enumerate(range(offset, len(binary), record_size)):
        thread, encoded, cpu, wall = struct.unpack_from('<HIII', binary, position)
        method_id, action = encoded & ~3, encoded & 3
        if method_id not in methods or action == 3:
            raise ValueError('unknown trace method or action')
        method = methods[method_id]
        is_root = method[0] == ROOT_CLASS and method[1] in ROOT_METHODS
        label = method[0] + '.' + method[1] + method[2]
        if action == 0:
            if selected(method):
                counts[label] += 1
            if is_root:
                if thread in active:
                    raise ValueError('nested root contract unsupported')
                row = dict(method=method[1], thread=thread, begin_sequence=sequence,
                           selected_calls=[], stack=[method_id], start_wall=wall)
                active[thread] = row
                roots.append(row)
            elif thread in active:
                active[thread]['stack'].append(method_id)
            if thread in active and selected(method):
                active[thread]['selected_calls'].append(dict(method=label, sequence=sequence))
        elif thread in active:
            row = active[thread]
            if not row['stack'] or row['stack'].pop() != method_id:
                raise ValueError('root call stack mismatch')
            if not row['stack']:
                row.update(end_sequence=sequence, end_wall=wall,
                           wall_microseconds=(wall - row['start_wall']) & 0xffffffff,
                           completion='returned' if action == 1 else 'unwound')
                row.pop('stack')
                active.pop(thread)
    if active:
        raise ValueError('incomplete observed root')
    return dict(schema='e2.1-art-method-summary.v1', trace_sha256=hashlib.sha256(raw).hexdigest(),
                trace_bytes=len(raw), records=record_count, selected_counts=dict(sorted(counts.items())),
                roots=roots, coverage='recorded post-cold-ready window only; other entry/exit boundaries not asserted',
                return_values='unknown; ART method trace records control flow, not values',
                no_vm_verified=False, training_admissible=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = parse(args.trace.read_bytes())
    with args.output.open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps(dict(records=result['records'], roots=len(result['roots']), counts=result['selected_counts'])))


if __name__ == '__main__':
    main()
