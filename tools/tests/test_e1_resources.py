import ctypes
from concurrent.futures import ThreadPoolExecutor
import shutil
import struct
import subprocess
import sys
from pathlib import Path
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from e1_apk_index import build_index

TOOLS = Path(__file__).parents[1]

def archive(tmp_path):
    path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(path, "w") as target:
        target.writestr("assets/stored", b"fixture-stored-content", compress_type=0)
        target.writestr("assets/compressed", b"fixture-compressed" * 1000, compress_type=8)
        target.writestr("assets/empty", b"", compress_type=8)
    return path

@pytest.mark.parametrize("name", ["../escape", "a//b", "/absolute", "a/./b", "tab\tname", "a\\b"])
def test_index_rejects_invalid_paths(tmp_path, name):
    path = tmp_path / "invalid.zip"
    with zipfile.ZipFile(path, "w") as target:
        target.writestr("assets/" + name, b"fixture")
    with pytest.raises(ValueError):
        build_index(path)

def test_index_rejects_duplicates(tmp_path):
    path = archive(tmp_path)
    with pytest.warns(UserWarning), zipfile.ZipFile(path, "a") as target:
        target.writestr("assets/stored", b"duplicate")
    with pytest.raises(ValueError):
        build_index(path)

def test_index_rejects_local_directory_disagreement(tmp_path):
    path = archive(tmp_path)
    content = bytearray(path.read_bytes())
    struct.pack_into("<H", content, 8, 8)
    path.write_bytes(content)
    with pytest.raises(ValueError):
        build_index(path)

@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_index_rejects_other_compression(tmp_path, compression):
    path = tmp_path / "unsupported.zip"
    with zipfile.ZipFile(path, "w", compression=compression) as target:
        target.writestr("assets/fixture", b"fixture")
    with pytest.raises(ValueError):
        build_index(path)

@pytest.fixture
def provider(tmp_path):
    compiler = shutil.which("cc")
    assert compiler, "C compiler required; do not silently skip provider tests"
    library = tmp_path / "provider.dylib"
    subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O2",
        "-shared", "-fPIC", str(TOOLS / "e1_apk_provider.c"), "-lz", "-pthread",
        "-o", str(library)], check=True, capture_output=True)
    api = ctypes.CDLL(str(library), use_errno=True)
    signatures = {
        "initialize": (ctypes.c_void_p, [ctypes.c_char_p] * 3),
        "AAssetManager_fromJava": (ctypes.c_void_p, [ctypes.c_void_p] * 2),
        "AAssetManager_open": (ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]),
        "AAsset_read": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]),
        "AAsset_seek64": (ctypes.c_int64, [ctypes.c_void_p, ctypes.c_int64, ctypes.c_int]),
        "AAsset_getLength64": (ctypes.c_int64, [ctypes.c_void_p]),
        "AAsset_getRemainingLength64": (ctypes.c_int64, [ctypes.c_void_p]),
        "AAsset_openFileDescriptor64": (ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_int64)]),
        "AAsset_close": (None, [ctypes.c_void_p]),
        "metric": (ctypes.c_uint64, [ctypes.c_uint]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(api, "e1_" + name)
        function.restype, function.argtypes = result, arguments
    path = archive(tmp_path)
    rows = build_index(path)
    index = tmp_path / "index.tsv"
    index.write_text("".join("\t".join(map(str, row)) + "\n" for row in rows))
    return api, path, index, tmp_path / "audit.tsv", rows

def initialize(provider):
    api, path, index, audit, rows = provider
    context = api.e1_initialize(bytes(path), bytes(index), bytes(audit))
    assert context
    assert api.e1_AAssetManager_fromJava(None, None) == context
    return api, context, rows

@pytest.mark.parametrize("name", ["stored", "compressed", "empty"])
def test_provider_bytes_seek_length_descriptor(provider, name):
    import os
    api, context, rows = initialize(provider)
    with zipfile.ZipFile(provider[1]) as source:
        expected = source.read("assets/" + name)
    handle = api.e1_AAssetManager_open(context, name.encode(), 1)
    assert handle
    assert api.e1_AAsset_getLength64(handle) == len(expected)
    output = ctypes.create_string_buffer(len(expected) + 10)
    assert api.e1_AAsset_read(handle, output, len(output)) == len(expected)
    assert output.raw[:len(expected)] == expected
    assert api.e1_AAsset_read(handle, output, 1) == 0
    assert api.e1_AAsset_getRemainingLength64(handle) == 0
    assert api.e1_AAsset_seek64(handle, 0, 0) == 0
    assert api.e1_AAsset_seek64(handle, 1, 2) == -1
    assert api.e1_AAsset_getRemainingLength64(handle) == len(expected)
    assert api.e1_AAsset_seek64(handle, -1, 0) == -1
    assert api.e1_AAsset_seek64(handle, 0, 99) == -1
    start, length = ctypes.c_int64(-1), ctypes.c_int64(-1)
    descriptor = api.e1_AAsset_openFileDescriptor64(handle, ctypes.byref(start), ctypes.byref(length))
    if name == "stored":
        assert descriptor >= 0 and length.value == len(expected)
        assert os.pread(descriptor, length.value, start.value) == expected
        os.close(descriptor)
    else:
        assert descriptor == -1
    api.e1_AAsset_close(handle)
    assert api.e1_metric(2) == 0
    assert api.e1_metric(1) == 0

@pytest.mark.parametrize("line", ["", "a\t0\t0\t0\t0\t0\textra\n",
    "../a\t0\t0\t0\t0\t0\n", "a\t0\t0\t0\t9\t0\n",
    "a\t-1\t0\t0\t0\t0\n", "a\t0\t1\t0\t0\t0\n",
    "a\t0\t0\t0\t0\t4294967296\n"])
def test_provider_rejects_malformed_index(provider, line):
    api, path, index, audit, rows = provider
    index.write_text(line)
    assert not api.e1_initialize(bytes(path), bytes(index), bytes(audit))

def test_provider_missing_invalid_crc_and_no_leaks(provider):
    api, context, rows = initialize(provider)
    assert not api.e1_AAssetManager_open(context, b"missing", 0)
    assert api.e1_metric(1) == 0
    assert not api.e1_AAssetManager_open(context, b"../invalid", 0)
    assert not api.e1_AAssetManager_open(None, b"stored", 0)
    assert not api.e1_AAssetManager_open(context, b"stored", 4)
    stored = next(row for row in rows if row[0] == "stored")
    with provider[1].open("r+b") as target:
        target.seek(stored[1])
        target.write(b"X")
    assert not api.e1_AAssetManager_open(context, b"stored", 0)
    assert api.e1_metric(1) == 4 and api.e1_metric(2) == 0

def test_independent_handles_concurrent_reads(provider):
    api, context, rows = initialize(provider)
    def read_one(position):
        handle = api.e1_AAssetManager_open(context, b"compressed", position % 4)
        assert handle
        output = ctypes.create_string_buffer(18)
        assert api.e1_AAsset_read(handle, output, 18) == 18
        api.e1_AAsset_close(handle)
        return output.raw
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert len(set(executor.map(read_one, range(64)))) == 1
    assert api.e1_metric(2) == 0 and api.e1_metric(1) == 0
