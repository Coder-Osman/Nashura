import ctypes
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest


class Receipt(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in ('changed', 'committed', 'failed', 'renamed')] + [
        ('error_o', ctypes.c_int32), ('error_p', ctypes.c_int32), ('backup_requested', ctypes.c_uint32)]


def bind(library):
    api = ctypes.CDLL(str(library))
    signatures = {
        'open': [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)],
        'read': [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(Receipt)],
        'write': [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(Receipt)],
        'close': [ctypes.c_void_p], 'free': [ctypes.c_void_p],
    }
    for name, arguments in signatures.items():
        function = getattr(api, 'e21_pn_' + name)
        function.argtypes = arguments
        function.restype = None if name == 'free' else ctypes.c_int
    return api


@pytest.fixture(scope='module')
def library(tmp_path_factory):
    target = tmp_path_factory.mktemp('pn-build') / 'pn.so'
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC', '-pthread',
                    str(Path(__file__).parents[1] / 'e2_1_pn_store.c'), '-o', str(target)],
                   capture_output=True, check=True)
    return target


@pytest.fixture
def store(library, tmp_path):
    api = bind(library)
    context = ctypes.c_void_p()
    assert api.e21_pn_open(os.fsencode(tmp_path), ctypes.byref(context)) == 0
    try:
        yield api, context, tmp_path
    finally:
        api.e21_pn_free(context)


def write(api, context, value):
    receipt = Receipt()
    assert api.e21_pn_write(context, value, ctypes.byref(receipt)) == 0
    return receipt


def read(api, context):
    receipt = Receipt()
    output = ctypes.create_string_buffer(4096)
    assert api.e21_pn_read(context, output, len(output), ctypes.byref(receipt)) == 0
    return output.value, receipt


def file_value(value):
    return b'NSPN\x01\0\0\0' + struct.pack('<I', len(value)) + value


def test_missing_same_empty_delete_and_closed(store):
    api, context, directory = store
    assert read(api, context)[0] == b''
    assert write(api, context, b'').changed == 0
    changed = write(api, context, b'17')
    assert (changed.changed, changed.committed, changed.renamed, changed.failed, changed.backup_requested) == (3, 3, 3, 0, 1)
    before = [(directory / name).stat().st_mtime_ns for name in ('pn-o.bin', 'pn-p.bin')]
    same = write(api, context, b'17')
    assert (same.changed, same.committed, same.backup_requested) == (0, 0, 0)
    assert before == [(directory / name).stat().st_mtime_ns for name in ('pn-o.bin', 'pn-p.bin')]
    assert write(api, context, None).committed == 3
    assert read(api, context)[0] == b''
    assert api.e21_pn_close(context) == 0
    output = ctypes.create_string_buffer(b'keep')
    receipt = Receipt()
    assert api.e21_pn_read(context, output, len(output), ctypes.byref(receipt)) == -2
    assert output.value == b'keep'
    assert api.e21_pn_write(context, b'late', ctypes.byref(receipt)) == -2
    assert api.e21_pn_close(context) == -2


@pytest.mark.parametrize('preferred', [b'', b'preferred'])
def test_priority_and_read_backfill(library, tmp_path, preferred):
    (tmp_path / 'pn-o.bin').write_bytes(file_value(b'older'))
    (tmp_path / 'pn-p.bin').write_bytes(file_value(preferred))
    api = bind(library)
    context = ctypes.c_void_p()
    assert api.e21_pn_open(os.fsencode(tmp_path), ctypes.byref(context)) == 0
    try:
        value, receipt = read(api, context)
        assert value == (preferred or b'older')
        assert receipt.changed == (0 if preferred else 2)
        assert receipt.committed == receipt.changed
        assert receipt.backup_requested == (0 if preferred else 1)
        assert (tmp_path / 'pn-p.bin').read_bytes() == file_value(value)
    finally:
        api.e21_pn_free(context)


def test_real_rename_failure_keeps_memory_and_does_not_retry_same_value(store):
    api, context, directory = store
    write(api, context, b'durable')
    durable_p = (directory / 'pn-p.bin').read_bytes()
    (directory / 'pn-o.bin').unlink()
    (directory / 'pn-o.bin').mkdir()
    failed = write(api, context, b'volatile')
    assert failed.changed == 3 and failed.failed == 1 and failed.error_o != 0
    assert failed.committed == 2 and failed.renamed == 2
    assert read(api, context)[0] == b'volatile'
    assert (directory / 'pn-p.bin').read_bytes() != durable_p
    assert write(api, context, b'volatile').changed == 0
    assert not list(directory.glob('*.tmp'))


def test_process_reopen_lease_and_real_permission_failure(library, store):
    api, context, directory = store
    write(api, context, b'durable')
    other = ctypes.c_void_p()
    assert api.e21_pn_open(os.fsencode(directory), ctypes.byref(other)) == -6
    assert not other.value
    assert api.e21_pn_close(context) == 0
    code = '''
import ctypes, json, os, sys
sys.path.insert(0, sys.argv[1])
from test_e2_1_pn_store import bind, read, write
api = bind(sys.argv[2]); context = ctypes.c_void_p()
assert api.e21_pn_open(os.fsencode(sys.argv[3]), ctypes.byref(context)) == 0
assert read(api, context)[0] == b'durable'
os.chmod(sys.argv[3], 0o500)
try:
    receipt = write(api, context, b'volatile')
    assert receipt.failed == 3 and receipt.committed == 0
    assert receipt.error_o and receipt.error_p
    assert read(api, context)[0] == b'volatile'
    assert write(api, context, b'volatile').changed == 0
finally:
    os.chmod(sys.argv[3], 0o700)
    api.e21_pn_free(context)
print(json.dumps({'real_permission_failure': True}))
'''
    if os.geteuid() == 0:
        pytest.skip('permission-failure experiment requires unprivileged host')
    result = subprocess.run([sys.executable, '-c', code, str(Path(__file__).parent), str(library), str(directory)],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['real_permission_failure']
    assert api.e21_pn_open(os.fsencode(directory), ctypes.byref(other)) == 0
    try:
        assert read(api, other)[0] == b'durable'
    finally:
        api.e21_pn_free(other)


@pytest.mark.parametrize('data', [b'', b'bad', file_value(b'x') + b'extra', file_value(b'x\0y'),
                                b'NSPN\x01\0\0\0' + struct.pack('<I', 4096)])
def test_corrupt_files_fail_without_defaults(library, tmp_path, data):
    (tmp_path / 'pn-p.bin').write_bytes(data)
    context = ctypes.c_void_p()
    assert bind(library).e21_pn_open(os.fsencode(tmp_path), ctypes.byref(context)) == -5
    assert not context.value


def test_symlink_rejected(library, tmp_path):
    (tmp_path / 'target').write_bytes(file_value(b'private'))
    (tmp_path / 'pn-p.bin').symlink_to(tmp_path / 'target')
    context = ctypes.c_void_p()
    assert bind(library).e21_pn_open(os.fsencode(tmp_path), ctypes.byref(context)) == -4
    assert not context.value


def test_bounds_unicode_and_per_store_competing_writes(store):
    api, context, _ = store
    receipt = Receipt()
    assert api.e21_pn_write(context, b'x' * 4096, ctypes.byref(receipt)) == -1
    values = [b'x' * 4095, '中文🙂'.encode(), b'opaque-not-decimal']
    def worker(value):
        for _ in range(12):
            write(api, context, value)
            assert read(api, context)[0] in values
    with ThreadPoolExecutor(3) as executor:
        list(executor.map(worker, values))
    output = ctypes.create_string_buffer(b'keep')
    assert api.e21_pn_read(context, output, 1, ctypes.byref(receipt)) == -3
    assert output.value == b'keep'
