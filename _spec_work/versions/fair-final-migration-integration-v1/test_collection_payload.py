import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from build_collection_payload import build_collection, INTEGRATION, SERIES, sha


PARENT = ROOT.parent / "fair-final-migration-cache-v1/native-deterministic-round0-1792641532086311000"


def unpack(package):
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}


def test_collection_preserves_training_identity_and_integrates_current_preflight(tmp_path):
    parent_files = unpack((PARENT / "payload.tar.gz").read_bytes())
    launch_sha = sha((PARENT / "launch.json").read_bytes())
    package, report = build_collection(PARENT, expected_launch_sha=launch_sha)
    assert build_collection(PARENT, expected_launch_sha=launch_sha) == (package, report)
    files = unpack(package)
    assert report["payload_sha256"] == sha(package)
    assert report["deterministic"] is True
    assert report["training_admissible"] is report["formal_g2_pass"] is False
    expected_changes = {INTEGRATION + name for name in
        ("collect_selected.py", "seed_registration.py", "seed_sources.py")}
    assert set(report["changes"]) == expected_changes
    for name, data in parent_files.items():
        if name != "deployment.json" and name not in expected_changes:
            assert files[name] == data
    identity = json.loads(files["deployment.json"])
    assert identity == report["source_sha256"]
    assert identity == {name: sha(data) for name, data in files.items() if name != "deployment.json"}
    source = files[INTEGRATION + "collect_selected.py"].decode()
    assert source.index("seed_evidence = validate_registration") < source.index("runtime_evidence = configure()")
    assert source.index("runtime_evidence = configure()") < source.index("teacher, _, _ = load_checkpoint")
    assert source.index("archive_registration(") < source.index("native = create_search_native")
    assert "deterministic_runtime_sha=" in source
    assert files[SERIES + "deterministic_runtime.py"] == parent_files[SERIES + "deterministic_runtime.py"]
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    result = subprocess.run([sys.executable, "-B", str(tmp_path / INTEGRATION / "collect_selected.py"), "--help"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--seed-source-artifacts" in result.stdout
    assert "--engineering-acceptance" in result.stdout


@pytest.mark.parametrize("damage", ["launch", "payload", "scope", "runtime", "manifest"])
def test_invalid_frozen_parent_is_rejected(tmp_path, damage):
    launch = json.loads((PARENT / "launch.json").read_text())
    package = (PARENT / "payload.tar.gz").read_bytes()
    if damage == "payload":
        package += b"changed"
    elif damage == "scope":
        launch["training_admissible"] = True
    elif damage == "runtime":
        launch["deterministic"] = False
    elif damage == "manifest":
        launch["source_sha256"]["private-plan.json"] = "0" * 64
    data = json.dumps(launch).encode()
    (tmp_path / "launch.json").write_bytes(data)
    (tmp_path / "payload.tar.gz").write_bytes(package)
    with pytest.raises(ValueError):
        build_collection(tmp_path, expected_launch_sha="0" * 64 if damage == "launch" else sha(data))


def test_legacy_parent_does_not_invent_deterministic_runtime():
    parent = ROOT / "native-selected-round0-1792629813912401000"
    package, report = build_collection(parent, expected_launch_sha=sha((parent / "launch.json").read_bytes()))
    files = unpack(package)
    assert report["deterministic"] is False
    assert SERIES + "deterministic_runtime.py" not in files
    assert b"runtime_evidence = configure()" not in files[INTEGRATION + "collect_selected.py"]
