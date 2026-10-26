import ctypes
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    output = tmp_path_factory.mktemp("e1-bridge") / "fixture.so"
    source = Path(__file__).with_name("e1_resource_fixture.c")
    subprocess.run(["cc", "-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror",
                    "-shared", "-fPIC", str(source), "-o", str(output)], check=True)
    library = ctypes.CDLL(str(output))
    library.fixture_read.argtypes = [ctypes.c_char_p, ctypes.c_int64, ctypes.c_int, ctypes.c_int,
                                     ctypes.POINTER(ctypes.c_int64)]
    library.fixture_exercise.argtypes = [ctypes.c_int64, ctypes.POINTER(ctypes.c_int64)]
    library.fixture_resolve.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    return library


@pytest.mark.parametrize("size,error,valid,expected", [(3, 0, 1, 3), (0, 0, 1, 0),
    (3, 1, 1, -2), (3, 0, 0, -1), (9, 0, 1, -1)])
def test_read_eof_errors_and_destruction(bridge, size, error, valid, expected):
    details = (ctypes.c_int64 * 4)()
    assert bridge.fixture_read(b"present", size, error, valid, details) == expected
    assert bridge.fixture_destroyed() == 1
    if expected >= 0:
        assert details[3] == 0


def test_missing_is_not_destroyed(bridge):
    assert bridge.fixture_read(b"missing", 3, 0, 1, (ctypes.c_int64 * 4)()) == -1
    assert bridge.fixture_destroyed() == 0


@pytest.mark.parametrize("size,expected", [(3, 0), (0, 0), (-1, -2), (2**63 - 1, -2)])
def test_seek_and_overflow_guard(bridge, size, expected):
    details = (ctypes.c_int64 * 24)()
    assert bridge.fixture_exercise(size, details) == expected
    assert bridge.fixture_destroyed() == 1
    if expected == 0:
        assert details[7] == size and details[9] == 1 and details[10] == 0
        assert details[11] == -1 and details[15] == -1


@pytest.mark.parametrize("text", [b"short", b"long-resource-name-for-heap-string"])
def test_string_layout_capacity_and_release(bridge, text):
    output = ctypes.create_string_buffer(64)
    assert bridge.fixture_resolve(text, output, len(output)) == len(text)
    assert output.value == text
    assert bridge.fixture_released() == int(len(text) >= 22)
    assert bridge.fixture_resolve(text, output, len(text)) == -1
    assert bridge.fixture_released() == int(len(text) >= 22)
    assert bridge.fixture_resolve(None, output, len(output)) == -3
