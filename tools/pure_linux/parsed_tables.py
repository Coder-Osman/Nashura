"""Validate read-only table diagnostics without granting catalog semantic parity."""
import hashlib
import json


def observations(text):
    phases = {name: {} for name in ['before', 'after']}
    summaries = {}
    errors = []
    active = 'before'
    for number, line in enumerate(text.splitlines(), 1):
        fields = line.split('\t')
        if fields[0] not in {'PARSED_TABLE', 'PARSED_TABLE_SUMMARY'}:
            continue
        try:
            phase = fields[1]
            if phase not in phases or phase != active:
                raise ValueError('unexpected phase order')
            if fields[0] == 'PARSED_TABLE':
                if len(fields) != 8 or phase in summaries:
                    raise ValueError('table row shape or order')
                slot, count, capacity, nonnull, csv_rows, csv_columns = map(int, fields[2:])
                if not 0 <= slot < 219 or slot in phases[phase]:
                    raise ValueError('duplicate or out of range slot')
                if not 0 <= count <= capacity <= 1000000 or nonnull != count:
                    raise ValueError('row pointer/count mismatch')
                if (csv_rows, csv_columns) != (-1, -1) and not (
                        0 <= csv_rows <= 1000000 and 0 <= csv_columns <= 100000):
                    raise ValueError('CSV dimensions')
                phases[phase][slot] = [slot, count, capacity, nonnull, csv_rows, csv_columns]
            else:
                if len(fields) != 4 or phase in summaries:
                    raise ValueError('summary shape or duplicate')
                totals = list(map(int, fields[2:]))
                if totals != [len(phases[phase]), sum(row[1] for row in phases[phase].values())]:
                    raise ValueError('summary totals')
                summaries[phase] = totals
                active = 'after' if phase == 'before' else 'done'
        except (ValueError, IndexError) as error:
            errors.append({'line': number, 'reason': str(error)})
    complete = not errors and set(summaries) == set(phases) and summaries['before'] == [0, 0] and summaries['after'][0] > 0
    canonical = json.dumps({phase: sorted(rows.values()) for phase, rows in phases.items()}, separators=(',', ':'))
    return {'complete': complete, 'errors': errors, 'summaries': summaries,
            'layout_sha256': hashlib.sha256(canonical.encode()).hexdigest(),
            'coverage': 'table layout, counts and nonnull row pointers; not cell contents or Android parity',
            'catalog_ready_pass': False}
