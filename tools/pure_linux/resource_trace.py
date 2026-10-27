"""Summarize per-process strace resource I/O; mappings do not prove CPU reads."""
import json
import posixpath
import re


CALL = re.compile(r"^(?:\d+:\d+:\d+\.\d+\s+)?(\w+)\((.*)\)\s+=\s+(.+)$")
OPEN = re.compile(r'^(.*?),\s*("(?:[^"\\]|\\.)*"),\s*(.*)$')


def analyze(lines, roots, cwd=None):
    if cwd is not None and not cwd.startswith('/'):
        raise ValueError('trace working directory must be absolute')
    descriptors = {}
    directories = {}
    unresolved_paths = []
    records = []
    missing = []
    exists_probes = []
    incomplete = []
    termination = []
    for number, line in enumerate(lines, 1):
        if '+++ exited with ' in line or '+++ killed by ' in line:
            termination.append(line.strip().split('+++')[1].strip())
        if '<unfinished ...>' in line or 'resumed>' in line:
            incomplete.append(number)
            continue
        match = CALL.match(line.strip())
        if not match:
            candidate = re.sub(r"^\d+:\d+:\d+\.\d+\s+", "", line.strip())
            if re.match(r"\w+\(", candidate):
                incomplete.append(number)
            continue
        name, arguments, result = match.groups()
        if name in {'chdir', 'fchdir'}:
            if not result.startswith('-1'):
                if name == 'fchdir':
                    cwd = directories.get(int(arguments, 0))
                else:
                    changed = json.loads(arguments)
                    cwd = changed if changed.startswith('/') else (
                        posixpath.normpath(posixpath.join(cwd, changed)) if cwd else None)
            continue
        if name in {'openat', 'newfstatat'}:
            opened = OPEN.match(arguments)
            if not opened:
                continue
            path = json.loads(opened.group(2))
            if not path.startswith('/'):
                parent = opened.group(1).strip()
                try:
                    parent_path = cwd if parent == 'AT_FDCWD' else directories.get(int(parent, 0))
                except ValueError:
                    parent_path = None
                if parent_path is None:
                    unresolved_paths.append({'line': number, 'path': path, 'dirfd': parent})
                    continue
                path = posixpath.normpath(posixpath.join(parent_path, path))
            if name == 'openat' and 'O_DIRECTORY' in opened.group(3):
                if not result.startswith('-1'):
                    directories[int(result.split()[0], 0)] = path
                continue
            labels = [(label, prefix.rstrip('/')) for label, prefix in roots.items()
                      if path.startswith(prefix.rstrip('/') + '/')]
            if not labels:
                continue
            if len(labels) != 1:
                raise ValueError('resource roots must not overlap')
            label, prefix = labels[0]
            if name == 'newfstatat':
                exists_probes.append({'root': label, 'path': path[len(prefix) + 1:], 'result': result,
                                      'line': number})
                continue
            if result.startswith('-1'):
                missing.append({'root': label, 'path': path[len(prefix) + 1:], 'result': result})
                continue
            descriptor = int(result.split()[0])
            if descriptor in descriptors:
                incomplete.append(number)
            row = {'root': label, 'path': path[len(prefix) + 1:], 'line': number, 'size': None,
                   'reads': [], 'mappings': [], 'errors': [], 'closed': False}
            records.append(row)
            descriptors[descriptor] = (row, 0)
            continue
        descriptor_match = re.match(r'(-?(?:0x[0-9a-f]+|\d+))(?:,|$)', arguments)
        if name in {'mmap', 'mmap2'}:
            fields = arguments.split(', ')
            if len(fields) != 6:
                continue
            descriptor = int(fields[4], 0)
        elif descriptor_match:
            descriptor = int(descriptor_match.group(1), 0)
        else:
            continue
        duplicated = name in {'dup', 'dup2', 'dup3'} or (
            name in {'fcntl', 'fcntl64'} and 'F_DUPFD' in arguments)
        if duplicated and descriptor in directories:
            if not result.startswith('-1'):
                directories[int(result.split()[0], 0)] = directories[descriptor]
        if name == 'close' and not result.startswith('-1'):
            directories.pop(descriptor, None)
        if descriptor not in descriptors:
            continue
        row, position = descriptors[descriptor]
        if result.startswith('-1'):
            row['errors'].append({'call': name, 'result': result})
            continue
        if name in {'fstat', 'newfstatat'}:
            size = re.search(r'\bst_size=(\d+)', arguments)
            if size:
                row['size'] = int(size.group(1))
        elif name in {'read', 'pread64'}:
            amount = int(result.split()[0], 0)
            start = int(arguments.rsplit(', ', 1)[1], 0) if name == 'pread64' else position
            row['reads'].append([start, start + amount])
            if name == 'read':
                descriptors[descriptor] = (row, position + amount)
        elif name == 'lseek':
            descriptors[descriptor] = (row, int(result.split()[0]))
        elif name in {'mmap', 'mmap2'}:
            start = int(fields[5], 0) * (4096 if name == 'mmap2' else 1)
            row['mappings'].append([start, start + int(fields[1], 0)])
        elif name == 'close':
            row['closed'] = True
            del descriptors[descriptor]
    for row in records:
        row['full_file_mapped'] = row['size'] is not None and [0, row['size']] in row['mappings']
        row['out_of_range'] = row['size'] is not None and any(
            start < 0 or end > row['size'] for start, end in row['reads'] + row['mappings'])
    return {'records': records, 'missing': missing, 'exists_probes': exists_probes, 'incomplete_lines': incomplete,
            'termination': termination,
            'complete_successful_trace': termination == ['exited with 0'] and not incomplete and not descriptors,
            'unresolved_relative_paths': unresolved_paths,
            'all_open_paths_resolved': not unresolved_paths,
            'open_descriptors_at_end': len(descriptors),
            'coverage': 'observed syscalls only; mmap is not proof of CPU byte consumption',
            'content_sha_verified_by_trace': False}
