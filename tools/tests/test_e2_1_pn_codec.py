import base64
import ctypes
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


TOOLS = Path(__file__).parents[1]
sys.path.insert(0, str(TOOLS))
from e2_1_pn_codec import PNCodec
from e2_1_pn_codec_matrix import run_codec_matrix


@pytest.fixture(scope='module')
def codec_build(tmp_path_factory):
    directory = tmp_path_factory.mktemp('explicit-codec')
    compiler = shutil.which('clang') or shutil.which('cc')
    candidates = [Path('/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib'),
                  Path('/usr/lib/x86_64-linux-gnu/libcrypto.so.3')]
    crypto = next((path for path in candidates if path.is_file()), None)
    if not compiler or not crypto:
        pytest.skip('C compiler and existing crypto provider required for engineering test')
    library = directory / 'codec.so'
    subprocess.run([compiler, '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror',
                    str(TOOLS / 'e2_1_pn_codec.c'), '-o', str(library)], check=True, capture_output=True)
    return library, crypto


@pytest.fixture
def codec(codec_build):
    instance = PNCodec(*codec_build)
    yield instance
    instance.close()


@pytest.mark.parametrize('iv,expected', [
    (None, 'f3eed1bdb5d2a03c064b5a7e3db181f8'),
    (bytes(range(16)), 'f58c4c04d6e5f1ba779eabfb5f7bfbd6')])
def test_nist_first_block_with_pkcs7(codec, iv, expected):
    key = bytes.fromhex('603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4')
    plain = bytes.fromhex('6bc1bee22e409f96e93d7e117393172a')
    encrypted = codec.transform(plain, key, iv)
    assert encrypted[:16].hex() == expected
    assert len(encrypted) == 32
    assert codec.transform(encrypted, key, iv, encrypt=False) == plain


@pytest.mark.parametrize('text', ['', 'a', 'b' * 15, 'c' * 16, 'd' * 17, '中文🙂\n<>&"', 'z' * 4095])
@pytest.mark.parametrize('iv', [None, bytes(range(16))])
def test_text_boundaries(codec, text, iv):
    key = bytes(range(32))
    encoded = codec.encode(text, key, iv)
    assert codec.decode(encoded, key, iv) == text
    assert len(base64.b64decode(encoded)) % 16 == 0


@pytest.mark.parametrize('data,key,iv,encrypt', [
    (b'x', b'short', None, True), (b'x', bytes(32), bytes(15), True),
    (bytes(4096), bytes(32), None, True), (b'', bytes(32), None, False),
    (b'bad', bytes(32), None, False), (bytes(4112), bytes(32), None, False)])
def test_invalid_arguments(codec, data, key, iv, encrypt):
    with pytest.raises(ValueError, match='-1'):
        codec.transform(data, key, iv, encrypt=encrypt)


def test_failure_does_not_publish_partial_plaintext(codec):
    key, iv = bytes(range(32)), bytes(range(16))
    encrypted = codec.transform(b'a' * 32, key, iv)
    damaged = bytearray(encrypted)
    damaged[-17] ^= 16
    output = ctypes.create_string_buffer(b'X' * 128)
    length = ctypes.c_size_t(999)
    status = codec.library.e21_codec_transform(codec.context, 1, 0, key, 32, iv, 16,
        bytes(damaged), len(damaged), output, 128, ctypes.byref(length))
    assert status == -4 and length.value == 0 and output.raw == b'X' * 128 + b'\0'


def test_capacity_mode_and_open_failures(codec, codec_build):
    length, output = ctypes.c_size_t(999), ctypes.create_string_buffer(32)
    for mode, capacity, expected in [(0, 1, -3), (2, 32, -1)]:
        assert codec.library.e21_codec_transform(codec.context, mode, 1, bytes(32), 32,
            None, 0, b'x', 1, output, capacity, ctypes.byref(length)) == expected
        assert length.value == 0
    for library in [b'relative', b'/missing/crypto', str(codec_build[0]).encode()]:
        context = ctypes.c_void_p(123)
        status = codec.library.e21_codec_open(library, ctypes.byref(context))
        assert status in (-1, -2) and context.value is None


def test_wrapper_closed_and_strict_decode(codec):
    with pytest.raises(Exception):
        codec.decode('%%%', bytes(32))
    with pytest.raises(TypeError):
        codec.transform('text', bytes(32))
    codec.close()
    codec.close()
    with pytest.raises(RuntimeError, match='closed'):
        codec.transform(b'x', bytes(32))


def test_matrix_restores_even_when_native_close_fails(codec_build, tmp_path):
    import json
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps(dict(keys=['00' * 32] * 2, iv='00' * 16)))
    calls, saved = [], {}

    class Observer:
        def command(self, operation):
            calls.append(operation)
            if operation == 'pn-begin':
                return 'pn-captured\t2'
            if operation == 'codec-info':
                raise ValueError('retained original failure')
            if operation == 'pn-restore':
                return 'pn-restored\t2'
            raise AssertionError(operation)

    class Native:
        def close(self):
            raise RuntimeError('retained close failure')

    config = dict(profile=str(profile), host_library=str(codec_build[0]), host_crypto_library=str(codec_build[1]))
    with pytest.raises(ValueError, match='original failure'):
        run_codec_matrix(Observer(), Native(), config, saved.__setitem__, lambda *args: [])
    assert calls[-1] == 'pn-restore'
    assert saved['pn-codec-matrix.json']['restored'] is True
    assert saved['pn-codec-matrix.json']['cleanup_errors']
