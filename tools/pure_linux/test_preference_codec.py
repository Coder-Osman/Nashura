import base64
import ctypes
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture(scope="module")
def codec(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    candidates = [Path("/opt/anaconda3/lib/libcrypto.3.dylib"),
                  Path("/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib"),
                  Path("/usr/lib/aarch64-linux-gnu/libcrypto.so.3"),
                  Path("/usr/lib/x86_64-linux-gnu/libcrypto.so.3")]
    crypto = next((path for path in candidates if path.is_file()), None)
    if not compiler or crypto is None:
        pytest.skip("C compiler and explicit crypto library required")
    source = Path(__file__).parent
    output = tmp_path_factory.mktemp("preference-codec") / "codec.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror", "-pthread",
                    str(source / "preference_codec.c"), str(source / "preference_cipher.c"),
                    "-o", str(output)], check=True)
    library = ctypes.CDLL(str(output))
    library.preference_codecs_open.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    library.preference_codecs_open_material.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                                       ctypes.POINTER(ctypes.c_void_p)]
    library.preference_codecs_transform.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                                   ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
    library.preference_codecs_close.argtypes = [ctypes.c_void_p]
    return library, str(crypto).encode()


def open_codecs(codec):
    handle = ctypes.c_void_p()
    assert codec[0].preference_codecs_open(codec[1], bytes(32) + bytes([1]) * 32,
                                          bytes(16), ctypes.byref(handle)) == 0
    return handle


def transform(codec, handle, slot, operation, value):
    output = ctypes.create_string_buffer(b"\xa5" * 8192, 8192)
    result = codec[0].preference_codecs_transform(handle, slot, operation, value, output, len(output))
    return result, output.value, output.raw


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 31, 32, 255, 4095, 4096])
def test_ascii_roundtrip_canonical_base64_and_slot_isolation(codec, size):
    handle = open_codecs(codec)
    try:
        plain = b"a" * size
        encrypted = []
        for slot in range(2):
            result, encoded, _ = transform(codec, handle, slot, 1, plain)
            assert result == 0 and b"\n" not in encoded
            assert base64.b64encode(base64.b64decode(encoded, validate=True)) == encoded
            assert len(base64.b64decode(encoded)) == (size // 16 + 1) * 16
            assert transform(codec, handle, slot, 2, encoded)[:2] == (0, plain)
            assert transform(codec, handle, slot, 1, plain)[1] == encoded
            encrypted.append(encoded)
        assert encrypted[0] != encrypted[1]
    finally:
        codec[0].preference_codecs_close(handle)


@pytest.mark.parametrize("value", [b"", b"a", b"abc", b"!!!!", b"AA=A", b"A===", b"====",
                                        b"AA==AAAA", b"AB==", b"AAB=", b"AA==\n"])
def test_noncanonical_domain_is_explicitly_unresolved_before_cipher(codec, value):
    handle = open_codecs(codec)
    try:
        result, _, raw = transform(codec, handle, 0, 2, value)
        assert result == -1 and raw == b"\xa5" * 8192
        encoded = transform(codec, handle, 0, 1, b"value")[1]
        assert transform(codec, handle, 0, 2, encoded)[:2] == (0, b"value")
    finally:
        codec[0].preference_codecs_close(handle)


def test_unicode_rejected_and_cipher_failure_distinct(codec):
    handle = open_codecs(codec)
    try:
        assert transform(codec, handle, 0, 1, "中文".encode())[0] == -1
        assert transform(codec, handle, 0, 2, base64.b64encode(bytes(16)))[0] == 1
    finally:
        codec[0].preference_codecs_close(handle)


def test_private_material_is_explicit_bounded_and_not_symlinked(codec, tmp_path):
    material = tmp_path / "material"
    material.write_bytes(bytes(80))
    material.chmod(0o600)
    handle = ctypes.c_void_p()
    assert codec[0].preference_codecs_open_material(codec[1], os.fsencode(material), ctypes.byref(handle)) == 0
    codec[0].preference_codecs_close(handle)
    alias = tmp_path / "alias"
    alias.symlink_to(material)
    assert codec[0].preference_codecs_open_material(codec[1], os.fsencode(alias), ctypes.byref(handle)) == -1
    for data, mode in [(bytes(79), 0o600), (bytes(81), 0o600), (bytes(80), 0o644)]:
        material.write_bytes(data)
        material.chmod(mode)
        assert codec[0].preference_codecs_open_material(codec[1], os.fsencode(material), ctypes.byref(handle)) == -1
        assert handle.value is None
