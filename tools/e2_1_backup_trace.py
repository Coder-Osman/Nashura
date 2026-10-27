"""Count explicitly named backup methods in a real ART trace, retaining unknown coverage."""

from collections import Counter
import hashlib
import struct

from verify_e2_gate import require


METHODS = {
    ('com.supercell.titan.GameApp', 'requestBackup', '()V'),
    ('android.app.backup.BackupManager', 'dataChanged', '()V'),
}


def parse_backup(raw):
    boundary = raw.find(b'*end\n')
    require(boundary >= 0, 'ART header missing')
    lines = raw[:boundary].decode('utf-8').splitlines()
    require(lines[:2] == ['*version', '3'] and 'data-file-overflow=false' in lines,
            'unsupported or overflowed ART trace')
    metadata = dict(line.split('=', 1) for line in lines if '=' in line and not line.startswith('0x'))
    methods = {}
    for line in lines[lines.index('*methods') + 1:]:
        fields = line.split('\t')
        require(len(fields) >= 4 and (fields[0].startswith('0x') or fields[0] == '0'), 'ART method row differs')
        identifier = int(fields[0], 0)
        require(identifier not in methods and identifier & 3 == 0, 'duplicate or unaligned ART method')
        methods[identifier] = tuple(fields[1:4])
    require(METHODS <= set(methods.values()), 'named backup methods absent')
    binary = raw[boundary + 5:]
    require(len(binary) >= 32, 'short ART binary header')
    magic, version, offset, _, size = struct.unpack_from('<4sHHQH', binary)
    require((magic, version, offset, size, metadata.get('clock')) == (b'SLOW', 3, 32, 14, 'dual'),
            'unsupported ART binary layout')
    require((len(binary) - offset) % size == 0, 'partial ART record')
    records = (len(binary) - offset) // size
    require(records == int(metadata['num-method-calls']), 'ART record count differs')
    active, calls, unknown = {}, [], Counter()
    for sequence, position in enumerate(range(offset, len(binary), size)):
        thread, encoded, _, wall = struct.unpack_from('<HIII', binary, position)
        identifier, action = encoded & ~3, encoded & 3
        require(thread > 0 and action != 3, 'invalid ART thread/action')
        method = methods.get(identifier)
        if method is None:
            unknown[identifier] += 1
        if method not in METHODS:
            continue
        stack = active.setdefault(thread, [])
        if action == 0:
            row = dict(method='.'.join(method[:2]) + method[2], thread=thread,
                       begin_sequence=sequence, start_wall=wall)
            calls.append(row)
            stack.append((identifier, row))
        else:
            require(stack and stack[-1][0] == identifier, 'backup method exit without matching entry')
            row = stack.pop()[1]
            row.update(end_sequence=sequence, end_wall=wall,
                       completion='returned' if action == 1 else 'unwound')
    require(not any(active.values()), 'incomplete backup method')
    return dict(schema='e2.1h-backup-art.v1', trace_sha256=hashlib.sha256(raw).hexdigest(),
                records=records, calls=calls,
                counts=dict(sorted(Counter(row['method'] for row in calls).items())),
                unnamed_method_count=len(unknown), unnamed_records=sum(unknown.values()),
                coverage='two explicitly named methods only; all unnamed method semantics remain unknown',
                backup_delivery='unknown')
