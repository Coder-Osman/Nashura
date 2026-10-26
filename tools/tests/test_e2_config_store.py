import ctypes
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def store(tmp_path):
    library = tmp_path / 'store.dylib'
    subprocess.run(['cc', '-std=c11', '-D_POSIX_C_SOURCE=200809L', '-Wall', '-Wextra', '-Werror',
                    '-shared', '-fPIC', '-pthread', str(Path(__file__).parents[1] / 'e2_config_store.c'),
                    '-o', str(library)], check=True, capture_output=True)
    api = ctypes.CDLL(str(library))
    signatures = {
        'create': (ctypes.c_void_p, []),
        'add': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]),
        'seal': (ctypes.c_int, [ctypes.c_void_p]),
        'read': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]),
        'write': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]),
        'close': (ctypes.c_int, [ctypes.c_void_p]),
        'free': (None, [ctypes.c_void_p]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(api, 'e2_store_' + name)
        function.restype, function.argtypes = result, arguments
    context = api.e2_store_create()
    assert context
    yield api, context
    api.e2_store_free(context)


def test_explicit_missing_empty_readonly_lifecycle(store):
    api, context = store
    output = ctypes.create_string_buffer(b'unchanged', 32)
    assert api.e2_store_read(context, b'missing', output, 32) == -2
    assert api.e2_store_add(context, b'empty', b'', 0) == 0
    assert api.e2_store_add(context, b'empty', b'overwritten', 0) == -2
    assert api.e2_store_seal(context) == 0
    assert api.e2_store_seal(context) == -2
    assert api.e2_store_add(context, b'late', b'value', 1) == -2
    assert api.e2_store_read(context, b'missing', output, 32) == -3
    assert output.value == b'unchanged'
    assert api.e2_store_read(context, b'empty', output, 32) == 0
    assert output.value == b''
    assert api.e2_store_write(context, b'empty', b'rejected') == -5
    assert api.e2_store_close(context) == 0
    assert api.e2_store_read(context, b'empty', output, 32) == -2
    assert api.e2_store_write(context, b'empty', b'value') == -2
    assert api.e2_store_close(context) == -2


def test_concurrent_writes_are_whole_values(store):
    api, context = store
    assert api.e2_store_add(context, b'writable', b'initial', 1) == 0
    assert api.e2_store_seal(context) == 0
    values = [str(index).encode() * 300 for index in range(1, 9)]
    def worker(value):
        for iteration in range(50):
            assert api.e2_store_write(context, b'writable', value) == 0
            output = ctypes.create_string_buffer(4096)
            assert api.e2_store_read(context, b'writable', output, len(output)) == 0
            assert output.value in values
    with ThreadPoolExecutor(8) as executor:
        list(executor.map(worker, values))
    assert api.e2_store_write(context, b'unknown', b'value') == -3
    assert api.e2_store_write(context, b'writable', b'x' * 4096) == -1
    output = ctypes.create_string_buffer(b'keep', 5)
    assert api.e2_store_read(context, b'writable', output, len(output)) == -4
    assert output.value == b'keep'
