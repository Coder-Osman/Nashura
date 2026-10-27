import ctypes
import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e2_1_pn_derive import PNDerivation


@pytest.fixture(scope='module')
def compiled(tmp_path_factory):
    directory = tmp_path_factory.mktemp('pn-derive')
    source = Path(__file__).parents[1] / 'e2_1_pn_derive.c'
    library = directory / 'derive.dylib'
    subprocess.run(['clang', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror',
                    str(source), '-o', str(library)], check=True)
    crypto = Path('/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib')
    if not crypto.exists():
        pytest.skip('requires existing host OpenSSL')
    return PNDerivation(library, crypto), directory


@pytest.mark.parametrize('length', [1, 19, 23, 24, 25, 48, 49, 255])
@pytest.mark.parametrize('table', [list(range(24)), [-2147483648, 2147483647, -1, 0] * 6])
def test_independent_hashlib_reference(compiled, length, table):
    wrapper, _ = compiled
    package = (b'example_0123456789.AbC.' * 13)[:length]
    identity = b'0123456789abcdef'
    iv_source = bytes(range(35))
    actual = wrapper.derive(identity, package, table, iv_source)
    transformed = bytes((((package[index] ^ table[index % 24]) & 31) + 48)
                        for index in reversed(range(len(package))))
    assert actual == dict(keys=[hashlib.sha256(identity).hexdigest(),
                                hashlib.sha256(transformed).hexdigest()], iv=iv_source[:16].hex())
    assert wrapper.derive(identity, package, table, iv_source) == actual


def invoke(wrapper, **changes):
    identity, package, table, source = b'0123456789abcdef', b'example.pkg', (ctypes.c_int32 * 24)(*range(24)), bytes(range(35))
    keys, iv = ctypes.create_string_buffer(b'\xa5' * 64, 64), ctypes.create_string_buffer(b'\xa5' * 16, 16)
    values = dict(crypto=wrapper.crypto, identity=identity, identity_length=len(identity), package=package,
        package_length=len(package), table=table, table_count=24, source=source, source_length=len(source),
        keys=keys, keys_capacity=64, iv=iv, iv_capacity=16)
    values.update(changes)
    status = wrapper.function(*values.values())
    return status, keys.raw, iv.raw


@pytest.mark.parametrize('changes', [dict(identity=None), dict(identity_length=15), dict(identity=b'G' * 16),
    dict(package=None), dict(package_length=0), dict(package_length=256), dict(package=b'invalid/pkg', package_length=11),
    dict(table=None), dict(table_count=23), dict(source=None), dict(source_length=15), dict(source_length=4096),
    dict(keys_capacity=63), dict(iv_capacity=15), dict(crypto=b'relative'), dict(crypto=None)])
def test_invalid_input_preserves_outputs(compiled, changes):
    assert invoke(compiled[0], **changes) == (-1, b'\xa5' * 64, b'\xa5' * 16)


def test_link_and_hash_failures_preserve_outputs(compiled):
    wrapper, directory = compiled
    assert invoke(wrapper, crypto=b'/nonexistent/e21-crypto') == (-2, b'\xa5' * 64, b'\xa5' * 16)
    for label, source, expected in [
            ('missing', 'int other(void) { return 0; }', -2),
            ('failure', '#include <stddef.h>\nunsigned char *SHA256(const unsigned char *src, size_t size, unsigned char *out) { (void)src; (void)size; out[0]=1; return 0; }', -3)]:
        path = directory / (label + '.c')
        path.write_text(source)
        library = path.with_suffix('.dylib')
        subprocess.run(['clang', '-shared', '-fPIC', str(path), '-o', str(library)], check=True)
        assert invoke(wrapper, crypto=str(library).encode()) == (expected, b'\xa5' * 64, b'\xa5' * 16)


def test_overlapping_output_and_source_rejected(compiled):
    wrapper, _ = compiled
    shared = ctypes.create_string_buffer(b'\xa5' * 96, 96)
    assert invoke(wrapper, keys=shared, iv=ctypes.byref(shared, 32))[0] == -1
    assert shared.raw == b'\xa5' * 96
    assert invoke(wrapper, keys=shared, source=ctypes.cast(shared, ctypes.c_char_p))[0] == -1
    assert shared.raw == b'\xa5' * 96


@pytest.mark.parametrize('changes', [dict(identity='0123456789abcdef'), dict(package='pkg'), dict(iv_source='iv'),
    dict(table=[True] * 24), dict(table=[2 ** 31] * 24), dict(table=tuple(range(24)))])
def test_python_explicit_types(compiled, changes):
    values = dict(identity=b'0123456789abcdef', package=b'example.pkg', table=list(range(24)), iv_source=bytes(35))
    values.update(changes)
    with pytest.raises((ValueError, TypeError)):
        compiled[0].derive(**values)
