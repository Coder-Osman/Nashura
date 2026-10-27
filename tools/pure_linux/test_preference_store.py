import ctypes
import errno
from pathlib import Path
import shutil
import subprocess

import pytest


CODEC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                        ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t)
EXCEPTION = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
BACKUP = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)
OBSERVER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint)
CONTAINS = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                           ctypes.c_char_p, ctypes.POINTER(ctypes.c_int))
GET = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                      ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t)
EDIT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint64))
CHANGE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint64,
                         ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)
COMMIT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_int))


class Options(ctypes.Structure):
    _fields_ = [("context", ctypes.c_void_p), ("codec", CODEC), ("exception", EXCEPTION),
                ("backup", BACKUP), ("observer", OBSERVER), ("file_context", ctypes.c_void_p)]


class Backend(ctypes.Structure):
    _fields_ = [("context", ctypes.c_void_p), ("codec", CODEC), ("contains", CONTAINS),
                ("get", GET), ("edit", EDIT), ("change", CHANGE), ("commit", COMMIT),
                ("exception", EXCEPTION), ("backup", BACKUP)]


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    source = Path(__file__).parent
    output = tmp_path_factory.mktemp("preference-store-build") / "store.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror", "-pthread",
                    *[str(source / name) for name in ("preference_store.c", "preference_files.c",
                                                      "preference_contract.c")], "-o", str(output)], check=True)
    result = ctypes.CDLL(str(output))
    result.preference_store_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(Options), ctypes.POINTER(ctypes.c_void_p)]
    result.preference_store_backend.argtypes = [ctypes.c_void_p, ctypes.POINTER(Backend)]
    result.preference_store_close.argtypes = [ctypes.c_void_p]
    result.preference_contract_write.argtypes = [ctypes.POINTER(Backend), ctypes.c_char_p, ctypes.c_char_p]
    result.preference_contract_read.argtypes = [ctypes.POINTER(Backend), ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
    return result


class Store:
    def __init__(self, library, directory):
        self.library = library
        self.events = []
        self.backups = 0
        self.failure = None
        self.path = directory

        def codec(context, slot, operation, value, output, capacity):
            transformed = (b"encoded:" + value) if operation < 2 else value.removeprefix(b"encoded:")
            if len(transformed) >= capacity:
                return -1
            ctypes.memmove(output, transformed + b"\0", len(transformed) + 1)
            return 0

        def backup(context):
            self.backups += 1
            return -1

        def observer(context, event, slot):
            self.events.append((event, slot))
            return errno.EIO if self.failure == (event, slot) else 0

        self.options = Options(None, CODEC(codec), EXCEPTION(lambda context, slot: -1),
                               BACKUP(backup), OBSERVER(observer), None)
        self.handle = ctypes.c_void_p()
        self.result = library.preference_store_open(str(directory).encode(), ctypes.byref(self.options),
                                                     ctypes.byref(self.handle))
        self.backend = Backend()
        if not self.result:
            assert library.preference_store_backend(self.handle, ctypes.byref(self.backend)) == 0

    def get(self, slot, key):
        output = ctypes.create_string_buffer(8192)
        assert self.backend.get(self.backend.context, slot, key, output, len(output)) == 0
        return output.value

    def put(self, slot, key, value):
        token, boolean = ctypes.c_uint64(), ctypes.c_int(-1)
        assert self.backend.edit(self.backend.context, slot, ctypes.byref(token)) == 0
        assert self.backend.change(self.backend.context, token.value, 0, key, value) == 0
        assert self.backend.commit(self.backend.context, token.value, ctypes.byref(boolean)) == 0
        assert self.backend.commit(self.backend.context, token.value, ctypes.byref(boolean)) == -1
        return boolean.value

    def close(self):
        assert self.library.preference_store_close(self.handle) == 0


def test_two_real_xml_maps_persist_before_unresolved_backup(library, tmp_path):
    original = b'<map><string name="unrelated">a&amp;b&#10;\xe4\xb8\xad</string></map>'
    for slot in range(2):
        (tmp_path / f"store-{slot}.xml").write_bytes(original)
    store = Store(library, tmp_path)
    assert store.result == 0
    key = b"LOADING_SCREEN_STATE_LABEL"
    assert library.preference_contract_write(ctypes.byref(store.backend), key, b"loading") == -1
    assert store.backups == 1
    for slot in range(2):
        assert store.get(slot, b"unrelated") == "a&b\n中".encode()
        assert store.get(slot, b"encoded:" + key) == b"encoded:loading"
        assert (1, slot) in store.events and (3, slot) in store.events
    store.close()
    reopened = Store(library, tmp_path)
    output = ctypes.create_string_buffer(8192)
    assert library.preference_contract_read(ctypes.byref(reopened.backend), key, output, len(output)) == 0
    assert output.value == b"loading"
    assert reopened.backups == 0
    assert library.preference_contract_write(ctypes.byref(reopened.backend), key, b"loading") == 0
    assert reopened.events == []
    reopened.close()


def test_false_commit_keeps_memory_until_restart_and_retries_dirty(library, tmp_path):
    store = Store(library, tmp_path)
    assert store.put(0, b"key", b"disk") == 1
    store.failure = (0, 0)
    assert store.put(0, b"key", b"memory") == 0
    assert store.get(0, b"key") == b"memory"
    assert b"disk" in (tmp_path / "store-0.xml").read_bytes()
    store.close()
    store = Store(library, tmp_path)
    assert store.get(0, b"key") == b"disk"
    store.failure = (1, 0)
    assert store.put(0, b"key", b"retry") == 0
    assert store.get(0, b"key") == b"retry"
    store.failure = None
    assert store.put(0, b"key", b"retry") == 1
    store.close()
    store = Store(library, tmp_path)
    assert store.get(0, b"key") == b"retry"
    store.close()


@pytest.mark.parametrize("xml", [b"", b"<map>", b'<map><int name="x" value="1" /></map>',
    b'<map><string name="x">a</string><string name="x">b</string></map>',
    b'<map><string name="x">a\x00b</string></map>', b'<map><string name="x">\xff</string></map>',
    b'<map><string name="x">&#0;</string></map>', b'<!DOCTYPE map><map />',
    b'<map><!--comment--></map>', b'<map><string name="x"><string /></string></map>',
    b'<map><string name="x">&unknown;</string></map>', b'<map />trailing'])
def test_unsupported_xml_fails_explicitly_without_replacing_bytes(library, tmp_path, xml):
    path = tmp_path / "store-0.xml"
    path.write_bytes(xml)
    store = Store(library, tmp_path)
    assert store.result == -1 and not store.handle.value
    assert path.read_bytes() == xml


def test_entities_empty_map_and_roundtrip(library, tmp_path):
    (tmp_path / "store-0.xml").write_bytes(
        b'<map><string name="a&#9;b">x\r\ny&#13;&#x1f642;</string><string name="empty" /></map>')
    store = Store(library, tmp_path)
    assert store.get(0, b"a\tb") == "x\ny\r🙂".encode()
    assert store.get(0, b"empty") == b""
    assert store.put(0, b'new<>&"\'', b'\t\r\n<>&"\'') == 1
    store.close()
    store = Store(library, tmp_path)
    assert store.get(0, b'new<>&"\'') == b'\t\r\n<>&"\''
    assert store.get(0, b"a\tb") == "x\ny\r🙂".encode()
    store.close()


def test_capacity_lock_and_thread_owner(library, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = Store(library, tmp_path)
    assert Store(library, tmp_path).result == -1
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(library.preference_store_backend, store.handle, ctypes.byref(Backend())).result() == -1
    for index in range(128):
        assert store.put(0, str(index).encode(), b"value") == 1
    token, boolean = ctypes.c_uint64(), ctypes.c_int()
    assert store.backend.edit(store.handle, 0, ctypes.byref(token)) == 0
    assert store.backend.change(store.handle, token.value, 0, b"over-capacity", b"value") == 0
    assert store.backend.commit(store.handle, token.value, ctypes.byref(boolean)) == -1
    store.close()
