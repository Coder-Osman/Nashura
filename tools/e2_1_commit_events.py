"""Validate delegated SharedPreferences returns without inferring Java/native thread IDs."""

from collections import Counter

from verify_e2_gate import require


HEADER = 'sequence\tthread\tnanoseconds\tstore\teditor\toperation\tresult'
STORE_METHODS = {'contains', 'getString', 'edit'}
EDITOR_METHODS = {'putString', 'remove', 'commit'}


def parse_events(text):
    lines = text.splitlines()
    require(len(lines) >= 2 and lines[0] == HEADER and lines[-1] == 'dropped\t0',
            'invalid observer header or dropped events')
    events = []
    stacks = {}
    editors = {}
    last_time = -1
    installed = set()
    for sequence, line in enumerate(lines[1:-1], 1):
        fields = line.split('\t')
        require(len(fields) == 7, 'invalid observer row')
        require(all(value.isdigit() for value in fields[:3] + [fields[4]]), 'invalid numeric field')
        number, thread, timestamp, editor = map(int, fields[:3] + [fields[4]])
        store, operation, result = fields[3], fields[5], fields[6]
        require(number == sequence and thread > 0 and timestamp >= last_time and store in {'o', 'p'},
                'observer sequence, clock, thread or store differs')
        last_time = timestamp
        row = dict(sequence=sequence, thread=thread, nanoseconds=timestamp, store=store,
                   editor=editor, operation=operation, result=result)
        if operation in {'install', 'uninstall'}:
            require(editor == 0 and not any(stacks.values()), 'lifecycle inside delegated call')
            if operation == 'install':
                require(store not in installed and result == 'android.app.SharedPreferencesImpl',
                        'invalid install event')
                installed.add(store)
            else:
                require(store in installed and result == 'restored', 'invalid uninstall event')
                installed.remove(store)
        else:
            require(store in installed and operation.count(':') == 1, 'uninstalled or unknown operation')
            method, phase = operation.split(':')
            require(method in (STORE_METHODS if editor == 0 else EDITOR_METHODS), 'unsupported delegated method')
            stack = stacks.setdefault(thread, [])
            if phase == 'enter':
                require(result == 'forward', 'call did not forward')
                if editor:
                    require(editors.setdefault(editor, (store, thread)) == (store, thread),
                            'editor identity reused across store/thread')
                stack.append((store, editor, method))
            else:
                require(phase == 'exit' and stack and stack.pop() == (store, editor, method),
                        'delegation threw, observer failed, or call stack mismatched')
                require(result in {'true', 'false'} if method == 'commit' else result == 'returned',
                        'invalid delegated return value')
        events.append(row)
    require(not any(stacks.values()), 'incomplete delegated call')
    return events


def delta(before, after):
    first, second = parse_events(before), parse_events(after)
    require(second[:len(first)] == first and len(second) >= len(first), 'snapshot prefix changed')
    result = second[len(first):]
    require(all(row['operation'] not in {'install', 'uninstall'} for row in result), 'case crosses lifecycle')
    return result


def commit_counts(events):
    return dict(sorted(Counter(row['store'] + ':' + row['result'] for row in events
                               if row['operation'] == 'commit:exit').items()))


def validate_case(row, expected):
    native = row['native']
    require(row['case'] == native['case'] and native['key'] == 'PN_POPUP'
            and native['exception_state']['before'] is False
            and native['exception_state']['after'] is False, 'wrong key/case or pending JNI exception')
    require(native['operation'] in {'read', 'write'}, 'unsupported native operation')
    if native['operation'] == 'write':
        require(native['return_type'] == 'void; raw register not interpreted as status',
                'write bridge status was inferred')
    events = delta(row['before'], row['after'])
    require(len({event['thread'] for event in events}) == 1, 'sequential case has multiple Java threads')
    counts = commit_counts(events)
    require(counts == expected, 'actual delegated commit result/count differs')
    return dict(case=row['case'], operation=native['operation'], commits=counts)


def sequential(rows):
    names = ['changed_write', 'changed_read', 'same_write', 'empty_write', 'empty_read', 'restore', 'restored_read']
    require([row['case'] for row in rows] == names, 'sequential cases differ')
    by_name = {row['case']: row['native'] for row in rows}
    require(by_name['changed_write']['input'] == by_name['changed_read']['result']['value']
            == by_name['same_write']['input'], 'changed read or same-value write differs')
    require(by_name['empty_write']['input'] == by_name['empty_read']['result']['value'] == '',
            'empty write/read differs')
    require(by_name['restore']['input'] == by_name['restored_read']['result']['value'], 'restore differs')
    result = []
    for index, row in enumerate(rows):
        if index:
            require(row['before'] == rows[index - 1]['after'], 'sequential window gap')
        result.append(validate_case(row, {'o:true': 1, 'p:true': 1}
                                    if row['case'] in {'changed_write', 'empty_write', 'restore'} else {}))
    return result


def failure_path(rows, trace):
    require([row['case'] for row in rows] == ['disk_denied_write', 'memory_after_failure'],
            'failure cases differ')
    require(rows[1]['before'] == rows[0]['after'], 'failure window gap')
    summaries = [validate_case(rows[0], {'o:false': 1, 'p:false': 1}), validate_case(rows[1], {})]
    require(rows[0]['native']['input'] == rows[1]['native']['result']['value'],
            'memory after commit(false) differs')
    roots = trace['roots']
    require(len(roots) == 2 and [row['method'] for row in roots] == ['storeKeyValue', 'getKeyValue']
            and all(row['completion'] == 'returned' for row in roots)
            and len({row['thread'] for row in roots}) == 1, 'failure ART roots differ')
    calls = roots[0]['selected_calls']
    require(sum(row['method'].endswith('.commit()Z') for row in calls) == 2, 'failure ART commit count differs')
    return dict(cases=summaries, bridge_error_channel='void; no typed error observed',
                memory_visibility='failed disk commit remains visible in this process',
                durability='not established by an in-memory read')


def competing_values(row, trace):
    result = row['result']
    workers = result['workers']
    require(result['status'] == 0 and len(workers) == 2
            and {worker['index'] for worker in workers} == {0, 1}
            and len({worker['thread'] for worker in workers}) == 2, 'race workers differ')
    require(all(worker['completed'] == 4 and worker['error'] == 0 and worker['detached'] is True
                and worker['unknown_reads'] == 0
                and worker['own_reads'] + worker['other_reads'] == 4 for worker in workers),
            'race worker error or unclassified read')
    events = delta(row['before'], row['after'])
    require(len({event['thread'] for event in events}) == 2, 'two Java observer streams required')
    roots = trace['roots']
    require(len(roots) == 16 and len({root['thread'] for root in roots}) == 2
            and all(root['completion'] == 'returned' and root['end_wall'] >= root['start_wall']
                    for root in roots), 'race ART windows differ')
    for thread in {root['thread'] for root in roots}:
        require([root['method'] for root in roots if root['thread'] == thread]
                == ['storeKeyValue', 'getKeyValue'] * 4, 'race ART root order differs')
    counts = commit_counts(events)
    require(not any(key.endswith(':false') for key in counts), 'unexpected race disk failure')
    art_commits = sum(call['method'].endswith('.commit()Z') for root in roots for call in root['selected_calls'])
    require(art_commits == sum(counts.values()) and art_commits > 0, 'ART/delegation commit counts differ')
    overlaps = sum(first['thread'] != second['thread']
                   and max(first['start_wall'], second['start_wall']) < min(first['end_wall'], second['end_wall'])
                   for index, first in enumerate(roots) for second in roots[index + 1:])
    require(overlaps > 0, 'no actual overlapping Java roots')
    return dict(workers=2, writes=8, reads=8, commits=counts, overlapping_art_root_pairs=overlaps,
                own_reads=sum(worker['own_reads'] for worker in workers),
                other_reads=sum(worker['other_reads'] for worker in workers), unknown_reads=0,
                thread_mapping='Linux TID, ART ID and Java ID not numerically equated',
                general_thread_safety='unknown; bounded competing-value experiment only')
