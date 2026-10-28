import ctypes
import errno
from pathlib import Path
import shutil
import subprocess

import pytest


VALUES = ["nullsroyale.rel.free", "nullsroyale.rel.free", "", "zero\0tail",
          "中文", "😀", "\ud800", "\udfff"]
RECORDS = ["00146e756c6c73726f79616c652e72656c2e66726565",
           "00146e756c6c73726f79616c652e72656c2e66726565", "0000",
           "000a7a65726fc0807461696c", "0006e4b8ade69687", "0006eda0bdedb880",
           "0003eda080", "0003edbfbf"]


@pytest.fixture(scope="module")
def journal(tmp_path_factory):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    source = Path(__file__).parent
    output = tmp_path_factory.mktemp("journal-library") / "journal.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                    str(source / "backup_journal.c"), "-o", str(output)], check=True)
    library = ctypes.CDLL(str(output), use_errno=True)
    library.backup_journal_append_utf16.argtypes = [ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint16), ctypes.c_size_t]
    library.backup_journal_append_utf16.restype = ctypes.c_int
    return library.backup_journal_append_utf16


def append(journal, path, value):
    payload = value.encode("utf-16-le", errors="surrogatepass")
    units = (ctypes.c_uint16 * (len(payload) // 2)).from_buffer_copy(payload)
    ctypes.set_errno(0)
    result = journal(str(path).encode(), units, len(units))
    return result, ctypes.get_errno()


def test_android_observed_vectors_duplicates_and_reopen(journal, tmp_path):
    path = tmp_path / "journal"
    expected = b""
    for value, record in zip(VALUES, RECORDS):
        assert append(journal, path, value)[0] == 0
        expected += bytes.fromhex(record)
        assert path.read_bytes() == expected
    assert append(journal, path, "reopened")[0] == 0
    assert path.read_bytes() == expected + bytes.fromhex("000872656f70656e6564")


@pytest.mark.parametrize("value", ["a" * 65536, "\0" * 32768, "中" * 21846])
def test_overflow_preserves_existing_content(journal, tmp_path, value):
    path = tmp_path / "journal"
    assert append(journal, path, "original")[0] == 0
    before = path.read_bytes()
    assert append(journal, path, value) == (-1, errno.EOVERFLOW)
    assert path.read_bytes() == before


@pytest.mark.parametrize("value,length", [("a" * 65535, 65535), ("中" * 21845, 65535)])
def test_exact_modified_utf8_size_limit(journal, tmp_path, value, length):
    path = tmp_path / "journal"
    assert append(journal, path, value)[0] == 0
    assert len(path.read_bytes()) == length + 2
    assert path.read_bytes()[:2] == b"\xff\xff"


def test_missing_parent_directory_and_regular_parent_errors(journal, tmp_path):
    assert append(journal, tmp_path / "missing" / "journal", "x") == (-1, errno.ENOENT)
    assert append(journal, tmp_path, "x") == (-1, errno.EISDIR)
    regular = tmp_path / "regular"
    regular.write_bytes(b"unrelated")
    assert append(journal, regular / "journal", "x") == (-1, errno.ENOTDIR)
    assert regular.read_bytes() == b"unrelated"


def test_empty_file_created_before_oversize_failure(journal, tmp_path):
    path = tmp_path / "journal"
    assert append(journal, path, "a" * 65536) == (-1, errno.EOVERFLOW)
    assert path.read_bytes() == b""


def test_explicit_invalid_input(journal, tmp_path):
    assert journal(None, None, 0) == -1
    assert ctypes.get_errno() == errno.EINVAL
    assert journal(str(tmp_path / "unused").encode(), None, 1) == -1
    assert ctypes.get_errno() == errno.EINVAL
    assert not (tmp_path / "unused").exists()


@pytest.fixture(scope="module")
def fault_library(tmp_path_factory):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    source = Path(__file__).parent
    output = tmp_path_factory.mktemp("journal-faults")
    redirects = ["-D" + name + "=journal_test_" + name for name in
                 ("open", "fsync", "fstat", "lseek", "write", "close")]
    subprocess.run([compiler, "-fPIC", "-Wall", "-Wextra", "-Werror", *redirects, "-c",
                    str(source / "backup_journal.c"), "-o", str(output / "journal.o")], check=True)
    subprocess.run([compiler, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                    str(output / "journal.o"), str(source / "backup_journal_fault_fixture.c"),
                    "-o", str(output / "fixture.so")], check=True)
    library = ctypes.CDLL(str(output / "fixture.so"), use_errno=True)
    library.backup_journal_append_utf16.argtypes = [ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint16), ctypes.c_size_t]
    library.backup_journal_append_utf16.restype = ctypes.c_int
    return library


@pytest.mark.parametrize("fault,error,expected_closes,expected_content", [
    (1, errno.EACCES, 0, b"original"),
    (2, errno.EIO, 1, b"original"),
    (3, errno.EIO, 1, b"original\x00\x01x"),
    (4, errno.EIO, 1, b"original"),
    (5, errno.ESPIPE, 1, b"original"),
    (6, errno.ENOSPC, 1, b"original"),
    (9, errno.EIO, 1, b"original"),
    (10, errno.ENOSPC, 1, b"original"),
    (11, errno.EBADF, 1, b"original\x00\x01x"),
    (12, errno.ENOSPC, 1, b"original\x00"),
])
def test_injected_failure_preserves_primary_error_and_side_effects(
        fault_library, tmp_path, fault, error, expected_closes, expected_content):
    path = tmp_path / "journal"
    path.write_bytes(b"original")
    fault_library.journal_fault_reset(fault)
    assert append(fault_library.backup_journal_append_utf16, path, "x") == (-1, error)
    assert fault_library.journal_close_count() == expected_closes
    assert path.read_bytes() == expected_content
    if fault == 12:
        assert fault_library.journal_sync_count() == 1


@pytest.mark.parametrize("fault", [7, 8])
def test_injected_eintr_and_short_write_retry(fault_library, tmp_path, fault):
    path = tmp_path / "journal"
    fault_library.journal_fault_reset(fault)
    assert append(fault_library.backup_journal_append_utf16, path, "x")[0] == 0
    assert path.read_bytes() == b"\x00\x01x"
    assert fault_library.journal_close_count() == 1
    assert fault_library.journal_sync_count() == 2
    assert fault_library.journal_write_count() == 2
