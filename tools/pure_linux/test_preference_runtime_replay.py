import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    candidates = [Path("/opt/anaconda3/lib/libcrypto.3.dylib"),
                  Path("/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib"),
                  Path("/usr/lib/aarch64-linux-gnu/libcrypto.so.3"),
                  Path("/usr/lib/x86_64-linux-gnu/libcrypto.so.3")]
    crypto = next((path for path in candidates if path.is_file()), None)
    if not compiler or crypto is None:
        pytest.skip("C compiler and explicit crypto library required")
    source = Path(__file__).parent
    output = tmp_path_factory.mktemp("runtime-replay") / "probe"
    names = ("preference_runtime_replay", "preference_codec", "preference_cipher",
             "preference_store", "preference_files", "preference_contract")
    subprocess.run([compiler, "-Wall", "-Wextra", "-Werror", "-pthread",
                    *(str(source / (name + ".c")) for name in names), "-o", str(output)], check=True)
    return output, crypto


def prepare(tmp_path):
    material = tmp_path / "material"
    material.write_bytes(bytes(80))
    material.chmod(0o600)
    directory = tmp_path / "store"
    directory.mkdir(mode=0o700)
    for slot in range(2):
        (directory / f"store-{slot}.xml").write_text("<map />\n")
    return material, directory


def test_replay_preserves_unresolved_backup_and_removes_values(replay, tmp_path):
    material, directory = prepare(tmp_path)
    result = subprocess.run([str(replay[0]), str(replay[1]), str(material), str(directory), "target"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    summary = json.loads(result.stdout)
    assert summary["target_result"] == summary["restore_result"] == -1
    assert summary["backup_requests"] == 2 and summary["decode_exceptions"] == 0
    assert summary["catalog_ready_pass"] is False and summary["libg_loaded"] is False
    for slot in range(2):
        assert b"<string name=" in (directory / f"target-{slot}.xml").read_bytes()
        assert (directory / f"restore-{slot}.xml").read_bytes() == (
            b"<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n<map />\n")
        assert (directory / f"restore-{slot}.xml").read_bytes() == (directory / f"store-{slot}.xml").read_bytes()
    repeat = subprocess.run([str(replay[0]), str(replay[1]), str(material), str(directory), "target"],
                            capture_output=True, text=True)
    assert repeat.returncode == 2


@pytest.mark.parametrize("case", ["empty", "unicode", "material", "nonempty_map"])
def test_rejects_out_of_scope_inputs(replay, tmp_path, case):
    material, directory = prepare(tmp_path)
    target = "target"
    if case == "empty":
        target = ""
    elif case == "unicode":
        target = "中文"
    elif case == "material":
        material.chmod(0o644)
    else:
        (directory / "store-0.xml").write_text('<map><string name="key">value</string></map>')
    result = subprocess.run([str(replay[0]), str(replay[1]), str(material), str(directory), target],
                            capture_output=True)
    assert result.returncode in (2, 64)
    assert not list(directory.glob("target-*.xml"))
