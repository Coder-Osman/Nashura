from resource_trace import analyze


def inspect(text):
    return analyze(text.splitlines(), {'update': '/resources/update'})


def test_mapping_and_descriptor_reuse():
    result = inspect('''12:01:00.000001 openat(AT_FDCWD, "/resources/update/a.csv", O_RDONLY) = 5
12:01:00.000002 fstat(5, {st_mode=S_IFREG|0644, st_size=12, ...}) = 0
12:01:00.000003 mmap(NULL, 12, PROT_READ, MAP_SHARED, 5, 0) = 0xffff0000
12:01:00.000004 close(5) = 0
12:01:00.000005 openat(AT_FDCWD, "/resources/update/b.csv", O_RDONLY) = 5
12:01:00.000006 fstat(5, {st_size=8}) = 0
12:01:00.000007 read(5, "abcd", 4) = 4
12:01:00.000008 pread64(5, "ef", 2, 6) = 2
12:01:00.000009 lseek(5, 1, SEEK_SET) = 1
12:01:00.000010 read(5, "xy", 2) = 2
12:01:00.000011 close(5) = 0''')
    first, second = result['records']
    assert first['full_file_mapped'] and first['reads'] == [] and first['closed']
    assert second['reads'] == [[0, 4], [6, 8], [1, 3]]
    assert not second['full_file_mapped'] and not second['out_of_range']
    assert result['open_descriptors_at_end'] == 0
    assert not result['content_sha_verified_by_trace']


def test_missing_failed_mapping_partial_and_truncated():
    result = inspect('''openat(AT_FDCWD, "/resources/update/missing", O_RDONLY) = -1 ENOENT (No such file)
openat(AT_FDCWD, "/resources/update/a", O_RDONLY) = 5
fstat(5, {st_size=12}) = 0
mmap(NULL, 12, PROT_READ, MAP_SHARED, 5, 0) = -1 ENOMEM (Out of memory)
mmap(NULL, 4, PROT_READ, MAP_SHARED, 5, 4) = 0xffff0000
read(5,  <unfinished ...>''')
    row = result['records'][0]
    assert len(result['missing']) == 1 and len(row['errors']) == 1
    assert row['mappings'] == [[4, 8]] and not row['full_file_mapped']
    assert not row['closed'] and result['open_descriptors_at_end'] == 1
    assert result['incomplete_lines'] == [6]


def test_out_of_range_and_namespace_boundary():
    result = inspect('''openat(AT_FDCWD, "/resources/update-other/a", O_RDONLY) = 4
openat(AT_FDCWD, "/resources/update/a", O_RDONLY) = 5
fstat(5, {st_size=12}) = 0
mmap(NULL, 14, PROT_READ, MAP_SHARED, 5, 0) = 0xffff0000
close(5) = 0''')
    assert len(result['records']) == 1
    assert result['records'][0]['out_of_range']
    assert not result['records'][0]['full_file_mapped']


def test_raw_read_arguments_and_exit():
    result = inspect('''openat(AT_FDCWD, "/resources/update/a", O_RDONLY) = 5
read(0x5, 0xffff0000, 0x4) = 0x4
pread64(0x5, 0xffff0000, 0x2, 0x6) = 0x2
close(5) = 0
+++ exited with 0 +++''')
    assert result['records'][0]['reads'] == [[0, 4], [6, 8]]
    assert result['termination'] == ['exited with 0']
    assert result['complete_successful_trace']


def test_missing_termination_and_truncated_syscall_are_incomplete():
    assert not inspect('close(5) = 0')['complete_successful_trace']
    result = inspect('12:00:00.000001 openat(AT_FDCWD, "/resources/update/a"\n+++ exited with 0 +++')
    assert result['incomplete_lines'] == [1]
    assert not result['complete_successful_trace']
    assert not inspect('+++ killed by SIGXFSZ +++')['complete_successful_trace']


def test_directory_duplication_and_relative_resource_io():
    result = inspect('''openat(AT_FDCWD, "/resources/update", O_RDONLY|O_DIRECTORY) = 4
fcntl(4, F_DUPFD_CLOEXEC, 0) = 5
openat(5, "nested", O_RDONLY|O_DIRECTORY) = 6
openat(6, "a.csv", O_RDONLY) = 7
fstat(7, {st_size=2}) = 0
read(0x7, 0xffff0000, 0x2) = 0x2
close(7) = 0
close(6) = 0
close(5) = 0
close(4) = 0
+++ exited with 0 +++''')
    assert result['records'][0]['path'] == 'nested/a.csv'
    assert result['records'][0]['reads'] == [[0, 2]]
    assert result['unresolved_relative_paths'] == []
    assert result['complete_successful_trace']


def test_untraced_directory_is_unknown():
    result = inspect('openat(5, "a.csv", O_RDONLY) = 6\n+++ exited with 0 +++')
    assert result['records'] == []
    assert result['unresolved_relative_paths'] == [{'line': 1, 'path': 'a.csv', 'dirfd': '5'}]
    assert not result['all_open_paths_resolved']
    assert result['complete_successful_trace']


def test_relative_working_directory_and_symbolic_dirfd_are_unknown():
    for parent in ['AT_FDCWD', 'UNKNOWN']:
        result = inspect(f'openat({parent}, "a.csv", O_RDONLY) = 6\n+++ exited with 0 +++')
        assert not result['all_open_paths_resolved']
        assert result['records'] == []


def test_bionic_dup_and_exists_fallback():
    result = inspect('''openat(AT_FDCWD, "/resources/update", O_RDONLY|O_DIRECTORY) = 4
dup(4) = 5
newfstatat(5, "missing.csv", 0xffff0000, 0) = -1 ENOENT (No such file)
newfstatat(5, "a.csv", {st_size=2}, 0) = 0
openat(5, "a.csv", O_RDONLY) = 6
close(6) = 0
close(5) = 0
close(4) = 0
+++ exited with 0 +++''')
    assert result['all_open_paths_resolved']
    assert result['records'][0]['path'] == 'a.csv'
    assert result['exists_probes'] == [
        {'root': 'update', 'path': 'missing.csv', 'result': '-1 ENOENT (No such file)', 'line': 3},
        {'root': 'update', 'path': 'a.csv', 'result': '0', 'line': 4}]
    assert result['exists_probes'][1]['line'] < result['records'][0]['line']


def test_working_directory_changes_and_relative_stat():
    text = '''newfstatat(AT_FDCWD, "a", {st_size=2}, 0) = 0
chdir("nested") = 0
openat(AT_FDCWD, "b", O_RDONLY) = 5
close(5) = 0
+++ exited with 0 +++'''
    result = analyze(text.splitlines(), {'update': '/resources/update'}, cwd='/resources/update')
    assert result['exists_probes'][0]['path'] == 'a'
    assert result['records'][0]['path'] == 'nested/b'
    assert result['all_open_paths_resolved']
