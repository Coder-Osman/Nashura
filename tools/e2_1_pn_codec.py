"""Explicit-key codec experiment; no original key derivation, XML writer or VM bridge."""

import base64
import ctypes
from pathlib import Path


class PNCodec:
    def __init__(self, library, crypto_library):
        self.library = ctypes.CDLL(str(Path(library).resolve(strict=True)))
        self.context = ctypes.c_void_p()
        self.library.e21_codec_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self.library.e21_codec_open.restype = ctypes.c_int
        self.library.e21_codec_transform.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t)]
        self.library.e21_codec_transform.restype = ctypes.c_int
        self.library.e21_codec_close.argtypes = [ctypes.c_void_p]
        self.library.e21_codec_close.restype = None
        status = self.library.e21_codec_open(str(Path(crypto_library).resolve(strict=True)).encode(),
                                            ctypes.byref(self.context))
        if status:
            raise RuntimeError(f'codec open failed: {status}')

    def transform(self, data, key, iv=None, *, encrypt=True):
        if not self.context.value:
            raise RuntimeError('codec closed')
        if not all(isinstance(value, bytes) for value in [data, key]) or (iv is not None and not isinstance(iv, bytes)):
            raise TypeError('explicit bytes required')
        output = ctypes.create_string_buffer(len(data) + 16)
        length = ctypes.c_size_t()
        status = self.library.e21_codec_transform(self.context, int(iv is not None), int(encrypt),
            key, len(key), iv, len(iv) if iv is not None else 0, data, len(data), output,
            len(output), ctypes.byref(length))
        if status:
            raise ValueError(f'codec transform failed: {status}')
        return output.raw[:length.value]

    def encode(self, text, key, iv=None):
        return base64.b64encode(self.transform(text.encode('utf-8'), key, iv)).decode('ascii')

    def decode(self, encoded, key, iv=None):
        return self.transform(base64.b64decode(encoded, validate=True), key, iv, encrypt=False).decode('utf-8')

    def close(self):
        if self.context.value:
            self.library.e21_codec_close(self.context)
            self.context.value = None
