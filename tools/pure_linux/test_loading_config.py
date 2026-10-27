import ctypes
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest


KEY = b"LOADING_SCREEN_STATE_LABEL"


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    output = tmp_path_factory.mktemp("loading-config-build") / "backend.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                    str(Path(__file__).with_name("loading_config.c")), "-pthread", "-o", str(output)], check=True)
    return output


def configure(path):
    backend = ctypes.CDLL(str(path))
    backend.loading_config_init.argtypes = [ctypes.c_char_p, ctypes.c_int]
    backend.loading_config_write.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint64)]
    backend.loading_config_read.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                                          ctypes.POINTER(ctypes.c_uint64)]
    return backend


@pytest.fixture
def backend(library, tmp_path):
    instance = configure(library)
    assert instance.loading_config_init(os.fsencode(tmp_path), 1) == 0
    yield instance
    assert instance.loading_config_finish() == 0


def read(backend, key=KEY):
    output = ctypes.create_string_buffer(4096)
    backups = ctypes.c_uint64(999)
    status = backend.loading_config_read(key, output, len(output), ctypes.byref(backups))
    return status, output.value, backups.value


def write(backend, value, key=KEY):
    backups = ctypes.c_uint64(999)
    status = backend.loading_config_write(key, value, ctypes.byref(backups))
    return status, backups.value


def test_changes_delete_and_backup_queue(backend):
    assert read(backend) == (0, b"", 0)
    assert write(backend, b"") == (0, 0)
    assert write(backend, b"datatables-init") == (0, 1)
    assert read(backend) == (0, b"datatables-init", 1)
    assert write(backend, b"datatables-init") == (0, 1)
    assert write(backend, b"") == (0, 2)
    assert read(backend) == (0, b"", 2)
    assert write(backend, None) == (0, 3)
    assert write(backend, None) == (0, 4)
    assert read(backend) == (0, b"", 4)


@pytest.mark.parametrize("slot,expected,backups", [(0, b"older", 1), (1, b"primary", 0)])
def test_priority_and_read_repair(backend, tmp_path, slot, expected, backups):
    path = tmp_path / "state"
    content = bytearray(path.read_bytes())
    for index, value in enumerate([b"older", b"primary" if slot else b""]):
        struct.pack_into("<I", content, 16 + index * 4, len(value) if value else 0xffffffff)
        content[24 + index * 4096:24 + index * 4096 + len(value)] = value
    path.write_bytes(content)
    assert read(backend) == (0, expected, backups)
    assert read(backend) == (0, expected, backups)


@pytest.mark.parametrize("value", [b"a" * 4096, b"line\nbreak", b"\xff"])
def test_reject_unknown_value_contract(backend, tmp_path, value):
    before = (tmp_path / "state").read_bytes()
    assert write(backend, value)[0] == -1
    assert (tmp_path / "state").read_bytes() == before


def test_unknown_key_and_maximum_length(backend, tmp_path):
    before = (tmp_path / "state").read_bytes()
    assert write(backend, b"value", b"UNKNOWN")[0] == -1
    assert read(backend, b"UNKNOWN")[0] == -1
    assert (tmp_path / "state").read_bytes() == before
    assert write(backend, b"a" * 4095) == (0, 1)
    assert read(backend) == (0, b"a" * 4095, 1)


@pytest.mark.parametrize("damage", ["truncated", "magic", "length", "terminator", "symlink"])
def test_corrupt_state_fails_closed(backend, tmp_path, damage):
    path = tmp_path / "state"
    content = bytearray(path.read_bytes())
    if damage == "truncated":
        content = content[:10]
    elif damage == "magic":
        content[0] = 0
    elif damage == "length":
        struct.pack_into("<I", content, 16, 5000)
    elif damage == "terminator":
        struct.pack_into("<I", content, 16, 1)
        content[24:26] = b"ab"
    elif damage == "symlink":
        path.rename(tmp_path / "other")
        path.symlink_to(tmp_path / "other")
    if damage != "symlink":
        path.write_bytes(content)
    assert read(backend)[0] == -1
    assert write(backend, b"new")[0] == -1


def test_pending_transaction_is_not_discarded(backend, tmp_path):
    before = (tmp_path / "state").read_bytes()
    (tmp_path / "pending").write_bytes(b"failure evidence")
    assert write(backend, b"value")[0] == -1
    assert (tmp_path / "state").read_bytes() == before
    assert (tmp_path / "pending").read_bytes() == b"failure evidence"


def test_cross_process_restore(library, tmp_path):
    script = """
import ctypes, os, sys
sys.path.insert(0, sys.argv[1])
from test_loading_config import configure, write, read
backend = configure(sys.argv[2])
assert backend.loading_config_init(os.fsencode(sys.argv[3]), int(sys.argv[4])) == 0
if sys.argv[4] == '1': assert write(backend, b'datatables-init') == (0, 1)
else: assert read(backend) == (0, b'datatables-init', 1)
assert backend.loading_config_finish() == 0
"""
    for fresh in [1, 0]:
        result = subprocess.run([sys.executable, "-c", script, str(Path(__file__).parent),
                                 str(library), str(tmp_path), str(fresh)], capture_output=True)
        assert result.returncode == 0, result.stderr
    backend = configure(library)
    assert backend.loading_config_init(os.fsencode(tmp_path), 1) == -1


def test_missing_restore_input(library, tmp_path):
    backend = configure(library)
    assert backend.loading_config_init(os.fsencode(tmp_path), 0) == -1
    assert not (tmp_path / "state").exists()
