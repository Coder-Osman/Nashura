import ctypes
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture(scope="module")
def cipher(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    candidates = [Path("/opt/anaconda3/lib/libcrypto.3.dylib"),
                  Path("/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib"),
                  Path("/usr/lib/aarch64-linux-gnu/libcrypto.so.3"),
                  Path("/usr/lib/x86_64-linux-gnu/libcrypto.so.3")]
    crypto = next((path for path in candidates if path.is_file()), None)
    if crypto is None:
        pytest.skip("explicit local OpenSSL library required; Android parity is separate")
    output = tmp_path_factory.mktemp("cipher") / "candidate.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror", "-pthread",
                    str(Path(__file__).with_name("preference_cipher.c")), "-o", str(output)], check=True)
    library = ctypes.CDLL(str(output))
    library.preference_cipher_open.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
    library.preference_cipher_transform.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    library.preference_cipher_close.argtypes = [ctypes.c_void_p]
    return library, str(crypto).encode()


def open_handle(cipher, key=bytes(32), iv=bytes(16)):
    library, crypto = cipher
    handle = ctypes.c_void_p()
    assert library.preference_cipher_open(crypto, key, len(key), iv, len(iv), ctypes.byref(handle)) == 0
    return handle


def transform(cipher, handle, operation, value, capacity=4160):
    output = ctypes.create_string_buffer(b"\xa5" * capacity, capacity)
    size = ctypes.c_size_t(123)
    result = cipher[0].preference_cipher_transform(handle, operation, value, len(value), output,
                                                  capacity, ctypes.byref(size))
    return result, output.raw[:size.value], size.value, output.raw


@pytest.mark.parametrize("length", [0, 1, 15, 16, 17, 31, 32, 127, 4095, 4096])
def test_boundaries_padding_and_successful_context_reset(cipher, length):
    handle = open_handle(cipher)
    try:
        plain = bytes(index % 251 for index in range(length))
        first = transform(cipher, handle, 1, plain)
        assert first[0] == 0
        assert first[2] == (length // 16 + 1) * 16
        assert transform(cipher, handle, 1, plain)[:3] == first[:3]
        assert transform(cipher, handle, 2, first[1])[:3] == (0, plain, length)
    finally:
        cipher[0].preference_cipher_close(handle)


def test_error_preserves_decrypt_state_but_not_other_ciphers(cipher):
    handle = open_handle(cipher)
    fresh = open_handle(cipher)
    try:
        encoded = transform(cipher, handle, 1, b"sample")[1]
        failure = transform(cipher, handle, 2, b"x")
        assert failure == (-3, b"", 0, b"\xa5" * 4160)
        assert transform(cipher, handle, 2, encoded)[0] == -3
        assert transform(cipher, fresh, 2, encoded)[:3] == (0, b"sample", 6)
        assert transform(cipher, handle, 0, b"key")[:3] == transform(cipher, fresh, 0, b"key")[:3]
        assert transform(cipher, handle, 1, b"sample")[1] == encoded
    finally:
        cipher[0].preference_cipher_close(handle)
        cipher[0].preference_cipher_close(fresh)


@pytest.mark.parametrize("operation,value,capacity,result", [
    (-1, b"valid", 64, -1), (3, b"valid", 64, -1), (1, b"x" * 4097, 4200, -1),
    (2, b"x" * 4113, 4200, -1), (1, b"value", 16, -4),
])
def test_rejected_request_leaves_output_and_context_unchanged(cipher, operation, value, capacity, result):
    handle = open_handle(cipher)
    fresh = open_handle(cipher)
    try:
        assert transform(cipher, handle, operation, value, capacity) == (result, b"", 0, b"\xa5" * capacity)
        assert transform(cipher, handle, 1, b"value")[:3] == transform(cipher, fresh, 1, b"value")[:3]
    finally:
        cipher[0].preference_cipher_close(handle)
        cipher[0].preference_cipher_close(fresh)


def test_missing_library_and_invalid_material_do_not_create_handle(cipher):
    library, crypto = cipher
    for path, key, iv, expected in [(b"relative", bytes(32), bytes(16), -1),
            (b"/nonexistent/library", bytes(32), bytes(16), -2),
            (crypto, bytes(31), bytes(16), -1), (crypto, bytes(32), bytes(15), -1)]:
        handle = ctypes.c_void_p(123)
        assert library.preference_cipher_open(path, key, len(key), iv, len(iv), ctypes.byref(handle)) == expected
        assert handle.value is None
