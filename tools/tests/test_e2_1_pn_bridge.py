import ctypes
from pathlib import Path
import subprocess

import pytest


READ = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t)
WRITE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p)
BACKUP = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)


class Backend(ctypes.Structure):
    _fields_ = [('context', ctypes.c_void_p), ('read', READ), ('write', WRITE), ('backup', BACKUP)]


class Receipt(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in ('reads', 'writes', 'changed', 'backup_returns')] + [
        ('failure', ctypes.c_int32)]


@pytest.fixture(scope='module')
def api(tmp_path_factory):
    target = tmp_path_factory.mktemp('pn-bridge') / 'bridge.so'
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                    str(Path(__file__).parents[1] / 'e2_1_pn_bridge.c'), '-o', str(target)], check=True)
    library = ctypes.CDLL(str(target))
    library.e21_bridge_read.argtypes = [ctypes.POINTER(Backend), ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(Receipt)]
    library.e21_bridge_write.argtypes = [ctypes.POINTER(Backend), ctypes.c_char_p, ctypes.POINTER(Receipt)]
    library.e21_bridge_read.restype = library.e21_bridge_write.restype = ctypes.c_int
    return library


class Store:
    def __init__(self, values, failure=None):
        self.values = list(values)
        self.events = []
        self.failure = failure
        self.backend = Backend(None, READ(self.read), WRITE(self.write), BACKUP(self.backup))

    def event(self, *event):
        self.events.append(event)
        return -17 if event == self.failure else 0

    def read(self, context, slot, output, capacity):
        status = self.event('read', slot)
        if not status:
            raw = self.values[slot] + b'\0'
            ctypes.memmove(output, raw, min(len(raw), capacity))
        return status

    def write(self, context, slot, value):
        status = self.event('write', slot)
        if not status:
            self.values[slot] = value
        return status

    def backup(self, context):
        return self.event('backup')


@pytest.mark.parametrize('values,expected,events', [
    ([b'', b''], b'', [('read', 1), ('read', 0)]),
    ([b'older', b'preferred'], b'preferred', [('read', 1)]),
    ([b'older', b''], b'older', [('read', 1), ('read', 0), ('write', 1), ('backup',)]),
    (['中文😀'.encode(), b''], '中文😀'.encode(), [('read', 1), ('read', 0), ('write', 1), ('backup',)]),
])
def test_priority_backfill_and_actual_callback_order(api, values, expected, events):
    store, receipt = Store(values), Receipt()
    output = ctypes.create_string_buffer(b'keep', 4096)
    assert api.e21_bridge_read(ctypes.byref(store.backend), output, len(output), ctypes.byref(receipt)) == 0
    assert output.value == expected and store.events == events
    assert receipt.backup_returns == int(('backup',) in events)


@pytest.mark.parametrize('values,value,events', [
    ([b'old', b'other'], b'new', [('read', 0), ('write', 0), ('read', 1), ('write', 1), ('backup',)]),
    ([b'new', b'other'], b'new', [('read', 0), ('read', 1), ('write', 1), ('backup',)]),
    ([b'new', b'new'], b'new', [('read', 0), ('read', 1)]),
    ([b'old', b'other'], b'', [('read', 0), ('write', 0), ('read', 1), ('write', 1), ('backup',)]),
])
def test_changed_write_order_and_no_redundant_backup(api, values, value, events):
    store, receipt = Store(values), Receipt()
    assert api.e21_bridge_write(ctypes.byref(store.backend), value, ctypes.byref(receipt)) == 0
    assert store.values == [value, value] and store.events == events
    assert receipt.backup_returns == int(('backup',) in events)


@pytest.mark.parametrize('failure', [('read', 1), ('read', 0), ('write', 1), ('backup',)])
def test_read_failure_stops_without_publishing_output(api, failure):
    store, receipt = Store([b'older', b''], failure), Receipt()
    output = ctypes.create_string_buffer(b'keep', 4096)
    assert api.e21_bridge_read(ctypes.byref(store.backend), output, len(output), ctypes.byref(receipt)) == -17
    assert output.value == b'keep' and store.events[-1] == failure and receipt.failure == -17
    if failure == ('backup',):
        assert store.values[1] == b'older' and receipt.changed == 2 and receipt.backup_returns == 0


def test_partial_write_failure_keeps_actual_prior_effects(api):
    store, receipt = Store([b'old', b'other'], ('write', 1)), Receipt()
    assert api.e21_bridge_write(ctypes.byref(store.backend), b'new', ctypes.byref(receipt)) == -17
    assert store.values == [b'new', b'other'] and receipt.changed == 1 and receipt.backup_returns == 0
    assert store.events == [('read', 0), ('write', 0), ('read', 1), ('write', 1)]


@pytest.mark.parametrize('value', [None, b'\xc0\x80', b'\xed\xa0\x80', b'\xf4\x90\x80\x80', b'\xf0\x9f', b'x' * 4096])
def test_invalid_input_fails_before_backend(api, value):
    store, receipt = Store([b'', b'']), Receipt()
    assert api.e21_bridge_write(ctypes.byref(store.backend), value, ctypes.byref(receipt)) == -1
    assert not store.events


@pytest.mark.parametrize('value', [b'\xff', b'\xf0\x9f', b'x' * 4096])
def test_invalid_backend_text_not_coerced_to_empty(api, value):
    store, receipt = Store([b'', value]), Receipt()
    output = ctypes.create_string_buffer(b'keep', 4096)
    assert api.e21_bridge_read(ctypes.byref(store.backend), output, len(output), ctypes.byref(receipt)) == -2
    assert output.value == b'keep' and store.events == [('read', 1)]


def test_maximum_valid_text_and_missing_backend(api):
    store, receipt = Store([b'', b'']), Receipt()
    assert api.e21_bridge_write(ctypes.byref(store.backend), b'x' * 4095, ctypes.byref(receipt)) == 0
    output = ctypes.create_string_buffer(4096)
    assert api.e21_bridge_read(ctypes.byref(store.backend), output, len(output), ctypes.byref(receipt)) == 0
    assert output.value == b'x' * 4095
    assert api.e21_bridge_read(None, output, len(output), ctypes.byref(receipt)) == -1
    assert api.e21_bridge_write(ctypes.byref(store.backend), b'a', None) == -1
