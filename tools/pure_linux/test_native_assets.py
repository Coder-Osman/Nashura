import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    output = tmp_path_factory.mktemp("assets-build") / "native_assets.so"
    source = Path(__file__).with_name("native_assets.c")
    subprocess.run([compiler, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                    str(source), "-pthread", "-o", str(output)], check=True)
    return output


def load_library(path):
    backend = ctypes.CDLL(str(path), use_errno=True)
    backend.nashura_assets_init.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    backend.AAssetManager_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    backend.AAssetManager_open.restype = ctypes.c_void_p
    backend.AAsset_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    backend.AAsset_seek64.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_int]
    backend.AAsset_seek64.restype = ctypes.c_int64
    for name in ["AAsset_getLength64", "AAsset_getRemainingLength64"]:
        function = getattr(backend, name)
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_int64
    backend.AAsset_openFileDescriptor64.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_int64),
                                                   ctypes.POINTER(ctypes.c_int64)]
    backend.AAsset_close.argtypes = [ctypes.c_void_p]
    return backend


def test_reads_and_lifetime(library, tmp_path):
    backend = load_library(library)
    (tmp_path / "stored").write_bytes(b"abc\x00def")
    (tmp_path / "compressed").write_bytes(b"zip")
    index = tmp_path / "index"
    index.write_text("7\t0\tstored\n3\t1\tcompressed\n")
    assert backend.nashura_assets_init(os.fsencode(tmp_path), os.fsencode(index)) == 0
    assert backend.nashura_assets_init(os.fsencode(tmp_path), os.fsencode(index)) == -1
    asset = backend.AAssetManager_open(None, b"stored", 2)
    assert asset
    assert backend.nashura_assets_finish() == -1
    buffer = ctypes.create_string_buffer(20)
    assert backend.AAsset_getLength64(asset) == 7
    assert backend.AAsset_read(asset, buffer, 3) == 3
    assert buffer.raw[:3] == b"abc"
    assert backend.AAsset_getRemainingLength64(asset) == 4
    assert backend.AAsset_seek64(asset, -1, os.SEEK_END) == 6
    assert backend.AAsset_read(asset, buffer, 20) == 1
    assert buffer.raw[:1] == b"f"
    assert backend.AAsset_read(asset, buffer, 1) == 0
    for offset, origin in [(-1, os.SEEK_SET), (8, os.SEEK_SET), (0, 987)]:
        assert backend.AAsset_seek64(asset, offset, origin) == -1
        assert backend.AAsset_getRemainingLength64(asset) == 0
    assert backend.AAsset_seek64(asset, 0, os.SEEK_SET) == 0
    assert backend.AAsset_read(asset, buffer, 20) == 7
    assert buffer.raw[:7] == b"abc\x00def"
    start, length = ctypes.c_int64(), ctypes.c_int64()
    descriptor = backend.AAsset_openFileDescriptor64(asset, ctypes.byref(start), ctypes.byref(length))
    assert descriptor >= 0 and start.value == 0 and length.value == 7
    assert os.pread(descriptor, 7, 0) == b"abc\x00def"
    os.close(descriptor)
    backend.AAsset_close(asset)
    asset = backend.AAssetManager_open(None, b"compressed", 3)
    assert backend.AAsset_openFileDescriptor64(asset, ctypes.byref(start), ctypes.byref(length)) == -1
    backend.AAsset_close(asset)
    assert not backend.AAssetManager_open(None, b"absent", 0)
    assert backend.nashura_assets_finish() == 0


@pytest.mark.parametrize("name", [b"", b"/absolute", b"../bad", b"a/../b", b"a//b",
                                      b"a/", b"./a", b"data:a", b"a\\b", b"a\tb"])
def test_invalid_paths(library, tmp_path, name):
    backend = load_library(library)
    index = tmp_path / "index"
    index.write_text("1\t0\tvalid\n")
    assert backend.nashura_assets_init(os.fsencode(tmp_path), os.fsencode(index)) == 0
    assert not backend.AAssetManager_open(None, name, 0)
    assert ctypes.get_errno() == 22
    assert backend.nashura_assets_finish() == 0


@pytest.mark.parametrize("content", [b"", b"1 0 a\n", b"-1\t0\ta\n", b"1\t2\ta\n",
    b"1\t0\t../a\n", b"1\t0\ta", b"1\t0\ta\x00junk\n", b"1\t0\ta\n1\t0\ta\n",
    b"9223372036854775808\t0\ta\n", b"+1\t0\ta\n", b"1\t00\ta\n"])
def test_reject_index(library, tmp_path, content):
    backend = load_library(library)
    index = tmp_path / "index"
    index.write_bytes(content)
    assert backend.nashura_assets_init(os.fsencode(tmp_path), os.fsencode(index)) != 0
    index.write_text("1\t0\tvalid\n")
    assert backend.nashura_assets_init(os.fsencode(tmp_path), os.fsencode(index)) == 0
    assert backend.nashura_assets_finish() == 0


@pytest.mark.parametrize("case", ["not_staged", "symlink", "wrong_size", "android_manager"])
def test_fail_closed(library, tmp_path, case):
    index = tmp_path / "index"
    index.write_text("1\t0\tasset\n")
    if case == "symlink":
        (tmp_path / "target").write_bytes(b"a")
        (tmp_path / "asset").symlink_to(tmp_path / "target")
    elif case in {"wrong_size", "android_manager"}:
        (tmp_path / "asset").write_bytes(b"aa")
    script = ("import sys; sys.path.insert(0, sys.argv[1]); from test_native_assets import load_library; "
              "b=load_library(sys.argv[2]); assert b.nashura_assets_init(sys.argv[3].encode(), "
              "sys.argv[4].encode())==0; b.AAssetManager_open(int(sys.argv[5]), b'asset', 0)")
    result = subprocess.run([sys.executable, "-c", script, str(Path(__file__).parent), str(library),
                             str(tmp_path), str(index), str(int(case == "android_manager"))],
                            capture_output=True)
    assert result.returncode == 95
    assert b"NATIVE_ASSET_FATAL" in result.stderr
