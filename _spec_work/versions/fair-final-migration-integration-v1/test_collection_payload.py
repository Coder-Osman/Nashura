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
BATCHED_PARENT = ROOT.parent / "fair-final-migration-batch-v1/native-batched-migration-1792664800317034000"
RUNTIME_PARENT = ROOT.parent / "fair-final-migration-cache-v1/native-deterministic-round0-1792647014623244000"


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
        ("collect_selected.py", "seed_registration.py", "seed_sources.py", "selected_collection_worker.py")}
    expected_changes.add("phase_snapshot_launcher.py")
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
    assert "--preflight-only" in result.stdout
    worker = subprocess.run([sys.executable, "-B", str(tmp_path / INTEGRATION / "selected_collection_worker.py"), "--help"],
        capture_output=True, text=True, timeout=30)
    assert worker.returncode == 0, worker.stderr
    assert "--deployment-sha" in worker.stdout


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


def test_batched_parent_requires_explicit_pinned_runtime_lineage(tmp_path):
    parent_sha = sha((BATCHED_PARENT / "launch.json").read_bytes())
    runtime_sha = sha((RUNTIME_PARENT / "launch.json").read_bytes())
    with pytest.raises(ValueError, match="runtime identity"):
        build_collection(BATCHED_PARENT, expected_launch_sha=parent_sha)
    package, report = build_collection(BATCHED_PARENT, expected_launch_sha=parent_sha,
        runtime_parent=RUNTIME_PARENT, expected_runtime_launch_sha=runtime_sha)
    assert build_collection(BATCHED_PARENT, expected_launch_sha=parent_sha,
        runtime_parent=RUNTIME_PARENT, expected_runtime_launch_sha=runtime_sha) == (package, report)
    files = unpack(package)
    original = unpack((BATCHED_PARENT / "payload.tar.gz").read_bytes())
    changed = set(report["changes"]) | {"deployment.json"}
    assert all(files[name] == data for name, data in original.items() if name not in changed)
    assert report["deterministic"] is True
    assert report["inherited_runtime_evidence"]["parent_launch_sha256"] == runtime_sha
    assert report["inherited_runtime_evidence"]["runtime_sha256"] == sha(files[SERIES + "deterministic_runtime.py"])
    assert b"runtime_evidence = configure()" in files[INTEGRATION + "collect_selected.py"]
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    result = subprocess.run([sys.executable, "-B", str(tmp_path / INTEGRATION / "collect_selected.py"), "--help"],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("damage", ["missing_sha", "missing_parent", "wrong_sha", "wrong_parent",
    "runtime_bytes", "lineage", "no_configuration", "explicit_false"])
def test_inherited_runtime_requires_verified_ancestor_and_training_configuration(tmp_path, damage):
    files = unpack((BATCHED_PARENT / "payload.tar.gz").read_bytes())
    launch = json.loads((BATCHED_PARENT / "launch.json").read_bytes())
    options = dict(runtime_parent=RUNTIME_PARENT,
        expected_runtime_launch_sha=sha((RUNTIME_PARENT / "launch.json").read_bytes()))
    if damage == "missing_sha":
        options["expected_runtime_launch_sha"] = None
    elif damage == "missing_parent":
        options["runtime_parent"] = None
    elif damage == "wrong_sha":
        options["expected_runtime_launch_sha"] = "0" * 64
    elif damage == "wrong_parent":
        options.update(runtime_parent=PARENT,
            expected_runtime_launch_sha=sha((PARENT / "launch.json").read_bytes()))
    elif damage == "runtime_bytes":
        files[SERIES + "deterministic_runtime.py"] += b"\n"
    elif damage == "lineage":
        launch["parent_launch_sha256"] = "f" * 64
    elif damage == "no_configuration":
        files["_spec_work/versions/fair-final-migration-selection-v1/train_round.py"] = b"pass\n"
    else:
        launch["deterministic"] = False
    identity = {name: sha(data) for name, data in files.items() if name != "deployment.json"}
    files["deployment.json"] = json.dumps(identity).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    package = buffer.getvalue()
    launch.update(payload_sha256=sha(package), source_sha256=identity)
    raw = json.dumps(launch).encode()
    (tmp_path / "launch.json").write_bytes(raw)
    (tmp_path / "payload.tar.gz").write_bytes(package)
    with pytest.raises(ValueError):
        build_collection(tmp_path, expected_launch_sha=sha(raw), **options)
