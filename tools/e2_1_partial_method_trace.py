"""Bound known ART root windows while retaining unnamed methods as opaque IDs."""

from collections import Counter
import hashlib
import struct

from e2_1_method_trace import ROOT_CLASS, ROOT_METHODS, selected
from verify_e2_gate import require


def parse_partial(raw, root_class=ROOT_CLASS, root_methods=ROOT_METHODS, allow_identical_metadata=False):
    boundary = raw.find(b'*end\n')
    require(boundary >= 0, 'trace text header missing')
    lines = raw[:boundary].decode('utf-8').splitlines()
    require(lines[:2] == ['*version', '3'] and 'data-file-overflow=false' in lines,
            'unsupported or overflowed trace')
    metadata = dict(line.split('=', 1) for line in lines if '=' in line and not line.startswith('0x'))
    methods, metadata_rows, duplicate_rows = {}, {}, 0
    for line in lines[lines.index('*methods') + 1:]:
        fields = line.split('\t')
        require(len(fields) >= 4 and (fields[0].startswith('0x') or fields[0] == '0'),
                'invalid trace method table')
        identifier = int(fields[0], 0)
        require(identifier & 3 == 0, 'unaligned trace method')
        if identifier in methods:
            require(allow_identical_metadata and metadata_rows[identifier] == fields[1:],
                    'duplicate or conflicting trace method')
            duplicate_rows += 1
            continue
        metadata_rows[identifier] = fields[1:]
        methods[identifier] = tuple(fields[1:4])
    require(root_methods <= {method[1] for method in methods.values() if method[0] == root_class},
            'known bridge roots absent from method table')
    binary = raw[boundary + 5:]
    require(len(binary) >= 32, 'short binary header')
    magic, version, offset, _, record_size = struct.unpack_from('<4sHHQH', binary)
    require((magic, version, offset, record_size, metadata.get('clock')) == (b'SLOW', 3, 32, 14, 'dual'),
            'unsupported trace binary layout')
    require((len(binary) - offset) % record_size == 0, 'partial trace record')
    records = (len(binary) - offset) // record_size
    require(records == int(metadata['num-method-calls']), 'method record count differs')
    active, roots, unknown = {}, [], {}
    counts = Counter()
    for sequence, position in enumerate(range(offset, len(binary), record_size)):
        thread, encoded, _, wall = struct.unpack_from('<HIII', binary, position)
        identifier, action = encoded & ~3, encoded & 3
        require(action != 3 and thread > 0, 'unsupported action or thread')
        method = methods.get(identifier)
        is_root = method is not None and method[0] == root_class and method[1] in root_methods
        is_selected = method is not None and selected(method)
        require(not (is_root and action and thread not in active), 'known root exit without entry')
        if method is None:
            row = unknown.setdefault(identifier, Counter())
            row[['entries', 'exits', 'unwinds'][action]] += 1
            row['records'] += 1
            row['inside_known_root'] += thread in active
        if action == 0:
            if is_root:
                require(thread not in active, 'nested known root')
                row = dict(method=method[1], thread=thread, begin_sequence=sequence, start_wall=wall,
                           selected_calls=[], stack=[], unnamed_records=0)
                active[thread] = row
                roots.append(row)
            if thread in active:
                active[thread]['stack'].append(identifier)
            if is_selected:
                label = method[0] + '.' + method[1] + method[2]
                counts[label] += 1
                if thread in active:
                    active[thread]['selected_calls'].append(dict(method=label, sequence=sequence))
        if thread in active:
            row = active[thread]
            row['unnamed_records'] += method is None
            if action:
                require(row['stack'] and row['stack'].pop() == identifier, 'known root opaque stack mismatch')
                if not row['stack']:
                    require(is_root, 'known root ended at another method')
                    row.update(end_sequence=sequence, end_wall=wall,
                               completion='returned' if action == 1 else 'unwound')
                    row.pop('stack')
                    active.pop(thread)
    require(not active, 'incomplete known root')
    return dict(schema='e2.1-partial-art-summary.v1', trace_sha256=hashlib.sha256(raw).hexdigest(),
                records=records, roots=roots, selected_counts=dict(sorted(counts.items())),
                identical_metadata_duplicates=duplicate_rows,
                unnamed_methods=[dict(identifier=identifier, **row) for identifier, row in sorted(unknown.items())],
                exhaustive_method_coverage=False,
                coverage='known method-table roots only; unnamed IDs retain opaque stack identity; semantics unknown',
                unnamed_method_origin='unknown; dynamic Proxy association is an unverified hypothesis',
                return_values='not recorded by ART; independent forwarding observer required')
