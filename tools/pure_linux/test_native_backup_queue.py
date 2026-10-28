import ctypes
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

import native_backup_consume as consumer


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    output = tmp_path_factory.mktemp("native-backup-build") / "queue.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror", "-pthread",
                    str(Path(__file__).with_name("native_backup_queue.c")), "-o", str(output)], check=True)
    result = ctypes.CDLL(str(output))
    result.native_backup_queue_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    result.native_backup_queue_request.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    result.native_backup_queue_close.argtypes = [ctypes.c_void_p]
    return result


def open_queue(library, path):
    handle = ctypes.c_void_p()
    assert library.native_backup_queue_open(os.fsencode(path), ctypes.byref(handle)) == 0
    return handle


def request(library, handle):
    created = ctypes.c_int(-1)
    result = library.native_backup_queue_request(handle, ctypes.byref(created))
    return result, created.value


@pytest.fixture
def spool(tmp_path):
    tmp_path.chmod(0o700)
    for slot in range(2):
        path = tmp_path / f"store-{slot}.xml"
        path.write_bytes(f'<map><string name="slot">{slot}</string></map>'.encode())
        path.chmod(0o660)
    (tmp_path / ".preference.lock").touch(mode=0o600)
    return tmp_path


def enqueue(library, spool):
    handle = open_queue(library, spool)
    assert request(library, handle) == (0, 1)
    assert library.native_backup_queue_close(handle) == 0
    return (spool / ".backup-pending").read_bytes()


def test_request_coalesces_and_is_not_delivery(library, spool):
    handle = open_queue(library, spool)
    assert request(library, handle) == (0, 1)
    marker = (spool / ".backup-pending").read_bytes()
    assert consumer.REQUEST.fullmatch(marker)
    assert request(library, handle) == (0, 0)
    assert (spool / ".backup-pending").read_bytes() == marker
    assert not list(spool.glob("backup-*"))
    assert library.native_backup_queue_close(handle) == 0
    handle = open_queue(library, spool)
    assert request(library, handle) == (0, 0)
    assert library.native_backup_queue_close(handle) == 0


def test_consume_copies_both_slots_then_acknowledges(library, spool):
    original = {f"store-{slot}.xml": (spool / f"store-{slot}.xml").read_bytes() for slot in range(2)}
    enqueue(library, spool)
    report = consumer.consume(spool)
    assert report["android_backup_parity"] == "NOT_VERIFIED"
    assert not (spool / ".backup-pending").exists()
    archived = spool / ("backup-" + report["request_id"])
    for name, data in original.items():
        assert (archived / name).read_bytes() == data == (spool / name).read_bytes()
    assert json.loads((archived / "manifest.json").read_text()) == report
    with pytest.raises(FileNotFoundError):
        consumer.consume(spool)
    second = enqueue(library, spool)
    assert consumer.REQUEST.fullmatch(second)[1].decode() != report["request_id"]


def test_queue_live_owner_excludes_consumer(library, spool):
    handle = open_queue(library, spool)
    assert request(library, handle) == (0, 1)
    with pytest.raises(BlockingIOError):
        consumer.consume(spool)
    assert (spool / ".backup-pending").exists()
    other = ctypes.c_void_p()
    assert library.native_backup_queue_open(os.fsencode(spool), ctypes.byref(other)) == -1
    assert library.native_backup_queue_close(handle) == 0


def test_cross_thread_and_process_rejected(library, spool):
    handle = open_queue(library, spool)
    results = []
    worker = threading.Thread(target=lambda: results.append(request(library, handle)))
    worker.start()
    worker.join()
    assert results == [(-1, -1)]
    child = os.fork()
    if not child:
        os._exit(0 if request(library, handle) == (-1, -1) else 1)
    assert os.waitpid(child, 0)[1] == 0
    assert not (spool / ".backup-pending").exists()
    assert library.native_backup_queue_close(handle) == 0


@pytest.mark.parametrize("data", [b"", b"unknown", b"nashura.native-backup.request.v1\n" + b"g" * 32 + b"\n",
                                      b"nashura.native-backup.request.v1\n" + b"0" * 32 + b"\nextra"])
def test_corrupt_pending_fails_closed(library, spool, data):
    path = spool / ".backup-pending"
    path.write_bytes(data)
    path.chmod(0o600)
    handle = open_queue(library, spool)
    assert request(library, handle)[0] == -1
    assert path.read_bytes() == data
    assert library.native_backup_queue_close(handle) == 0
    with pytest.raises(ValueError):
        consumer.consume(spool)
    assert path.read_bytes() == data


def test_incomplete_enqueue_is_preserved(library, spool):
    staged = spool / ".backup-request.tmp"
    staged.write_bytes(b"partial")
    handle = open_queue(library, spool)
    assert request(library, handle)[0] == -1
    assert library.native_backup_queue_close(handle) == 0
    with pytest.raises(ValueError, match="incomplete enqueue"):
        consumer.consume(spool)
    assert staged.read_bytes() == b"partial"


@pytest.mark.parametrize("name", [".backup.lock", ".backup-pending"])
def test_queue_symlinks_are_rejected(library, spool, name):
    outside = spool.parent / (spool.name + "-outside")
    outside.write_bytes(b"preserve")
    (spool / name).symlink_to(outside)
    if name == ".backup.lock":
        handle = ctypes.c_void_p()
        assert library.native_backup_queue_open(os.fsencode(spool), ctypes.byref(handle)) == -1
    else:
        handle = open_queue(library, spool)
        assert request(library, handle)[0] == -1
        assert library.native_backup_queue_close(handle) == 0
    assert outside.read_bytes() == b"preserve"


def test_directory_mode_and_symlink_rejected(library, spool):
    handle = ctypes.c_void_p()
    spool.chmod(0o755)
    assert library.native_backup_queue_open(os.fsencode(spool), ctypes.byref(handle)) == -1
    with pytest.raises(ValueError):
        consumer.consume(spool)
    spool.chmod(0o700)
    alias = spool.parent / (spool.name + "-alias")
    alias.symlink_to(spool)
    assert library.native_backup_queue_open(os.fsencode(alias), ctypes.byref(handle)) == -1


@pytest.mark.parametrize("failure", ["archive_write", "archive_verify", "ack"])
def test_failed_consumer_retains_pending_and_can_retry(library, spool, monkeypatch, failure):
    marker = enqueue(library, spool)
    with monkeypatch.context() as patch:
        if failure == "archive_write":
            patch.setattr(consumer, "write_file", lambda *args: (_ for _ in ()).throw(OSError("write fault")))
        elif failure == "archive_verify":
            patch.setattr(consumer, "verify_archive", lambda *args: (_ for _ in ()).throw(ValueError("verify fault")))
        else:
            patch.setattr(consumer.os, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ack fault")))
        with pytest.raises((ValueError, OSError)):
            consumer.consume(spool)
    assert (spool / ".backup-pending").read_bytes() == marker
    report = consumer.consume(spool)
    assert report["request_id"].encode() == consumer.REQUEST.fullmatch(marker)[1]
    assert not (spool / ".backup-pending").exists()


def test_archive_corruption_never_acknowledges(library, spool, monkeypatch):
    marker = enqueue(library, spool)
    with monkeypatch.context() as patch:
        patch.setattr(consumer.os, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ack fault")))
        with pytest.raises(OSError):
            consumer.consume(spool)
    archived = next(spool.glob("backup-*"))
    (archived / "store-0.xml").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="archive bytes differ"):
        consumer.consume(spool)
    assert (spool / ".backup-pending").read_bytes() == marker


def test_source_backup_or_missing_slot_never_acknowledges(library, spool):
    marker = enqueue(library, spool)
    (spool / "store-1.xml.bak").write_bytes(b"recover first")
    with pytest.raises(ValueError, match="recovery incomplete"):
        consumer.consume(spool)
    assert (spool / ".backup-pending").read_bytes() == marker


def test_preference_owner_excludes_consumer(library, spool):
    enqueue(library, spool)
    with (spool / ".preference.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            consumer.consume(spool)
    consumer.consume(spool)
