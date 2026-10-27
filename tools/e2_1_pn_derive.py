"""Explicit private source contract; no identity discovery or persistent writes."""

import ctypes
from pathlib import Path


class PNDerivation:
    def __init__(self, library, crypto_library):
        self.library = ctypes.CDLL(str(Path(library).resolve(strict=True)))
        self.crypto = str(Path(crypto_library).resolve(strict=True)).encode()
        self.function = self.library.e21_pn_derive
        self.function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int32), ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_size_t]
        self.function.restype = ctypes.c_int

    def derive(self, identity, package, table, iv_source):
        if not all(isinstance(value, bytes) for value in [identity, package, iv_source]):
            raise TypeError('explicit byte inputs required')
        if not isinstance(table, list) or any(type(value) is not int or not -(2 ** 31) <= value < 2 ** 31 for value in table):
            raise ValueError('explicit signed int32 table required')
        vector = (ctypes.c_int32 * len(table))(*table)
        keys, iv = ctypes.create_string_buffer(64), ctypes.create_string_buffer(16)
        status = self.function(self.crypto, identity, len(identity), package, len(package), vector,
            len(table), iv_source, len(iv_source), keys, 64, iv, 16)
        if status:
            raise ValueError(f'native derivation failed: {status}')
        return dict(keys=[keys.raw[:32].hex(), keys.raw[32:].hex()], iv=iv.raw.hex())
