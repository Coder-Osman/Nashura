from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest

from test_active_verifier import comparison, series


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("active_selection_handoff", ROOT / "prepare_selection.py")
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


def package(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in entries:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


@pytest.fixture
def parent(tmp_path, monkeypatch, comparison):
    parent = tmp_path / "parent"
    evidence = parent / "native-evidence"
    evidence.mkdir(parents=True)
    source = {name: b"FROZEN = True\n" for name in handoff.implementation_identity.__globals__["ACTIVE"]}
    source.update({"series.py": b"SERIES = True\n"})
    manifest = {name: handoff.digest(data) for name, data in source.items()}
    for name, data in source.items():
        path = evidence / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    source["deployment.json"] = json.dumps(manifest).encode()
    payload = package(source.items())
    (evidence / "payload.tar.gz").write_bytes(payload)
    (evidence / "deployment.json").write_bytes(source["deployment.json"])
    checkpoint = evidence / handoff.CHECKPOINT
    checkpoint.parent.mkdir()
    checkpoint.write_bytes((comparison / "trainable-reference.pt").read_bytes())
    state, identity, splits = handoff.checkpoint_state(checkpoint, handoff.sha(checkpoint))
    launch = dict(remote="/frozen/parent", payload_sha256=handoff.digest(payload), source_sha256=manifest)
    (parent / "launch.json").write_text(json.dumps(launch))
    (parent / "native-download-receipt.json").write_text(json.dumps(dict(
        files={handoff.CHECKPOINT: dict(sha256=handoff.sha(checkpoint))})))
    for name, value in (("selection-config.json", dict(patience=2, min_delta=1e-6)),
            ("loss-weights.json", dict(conditional_policy_kl=1.0))):
        (evidence / name).write_text(json.dumps(value))
    verification = dict(status="passed_active_archive_and_two_stage_checkpoint_scope",
        content=dict(native_schedule_verified=True, branches=dict(trainable=dict(state=state))),
        frozen_deployment_verified=True, dataset_identity=identity,
        split_rows={name: len(rows) for name, rows in splits.items()}, series_sha="e" * 64,
        training_admissible=False, formal_g2_pass=False, gates_closed=False)
    calls = []

    def verify(directory, **kwargs):
        calls.append((directory, kwargs))
        return deepcopy(verification)

    monkeypatch.setattr(handoff, "verify_archive", verify)
    monkeypatch.setattr(handoff, "implementation_identity", lambda directory: "f" * 64)
    return dict(path=parent, evidence=evidence, verification=verification, calls=calls,
        output=tmp_path / "selection", options=dict(launch_sha=handoff.sha(parent / "launch.json"),
            receipt_sha=handoff.sha(parent / "native-download-receipt.json"), data_roots=[tmp_path / "data"]))


def prepare(parent):
    return handoff.prepare(parent["path"], output=parent["output"], **parent["options"])


def test_handoff_keeps_frozen_payload_full_checkpoint_and_parent_unchanged(parent):
    original = {path.relative_to(parent["path"]): path.read_bytes()
        for path in parent["path"].rglob("*") if path.is_file()}
    report = prepare(parent)
    assert parent["calls"] == [(parent["path"], parent["options"])]
    assert report["status"] == "prepared_only_not_executed"
    assert report["max_updates"] is None and report["training_admissible"] is False
    assert report["parent_checkpoint_state"]["steps"] == 7
    assert (parent["output"] / "parent-checkpoint.pt").read_bytes() == original[
        Path("native-evidence") / handoff.CHECKPOINT]
    assert (parent["output"] / "payload.tar.gz").read_bytes() == original[Path("native-evidence/payload.tar.gz")]
    for name, metadata in report["files"].items():
        path = parent["output"] / name
        assert metadata == dict(sha256=handoff.sha(path), bytes=path.stat().st_size)
    assert {path.relative_to(parent["path"]): path.read_bytes()
        for path in parent["path"].rglob("*") if path.is_file()} == original
    assert handoff.read(parent["output"] / "selection-handoff.json") == report
    saved_sha = report.pop("handoff_sha")
    assert saved_sha == handoff.semantic_digest(report)
    with pytest.raises(ValueError, match="already exists"):
        prepare(parent)


@pytest.mark.parametrize("field,value", (("status", "failed"), ("training_admissible", True),
    ("formal_g2_pass", True), ("gates_closed", True), ("frozen_deployment_verified", False)))
def test_failed_or_promoted_parent_gate_rejected_before_writing(parent, field, value):
    parent["verification"][field] = value
    with pytest.raises(ValueError, match="independently verified"):
        prepare(parent)
    assert not parent["output"].exists()


def test_fixture_schedule_does_not_qualify_as_native_handoff(parent):
    parent["verification"]["content"]["native_schedule_verified"] = False
    with pytest.raises(ValueError, match="independently verified"):
        prepare(parent)
    assert not parent["output"].exists()


@pytest.mark.parametrize("name", ("launch.json", "native-download-receipt.json"))
def test_changed_external_parent_identity_is_rejected(parent, name):
    path = parent["path"] / name
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="pinned artifact"):
        prepare(parent)
    assert not parent["output"].exists()


def test_swapped_frozen_source_is_rejected(parent):
    path = parent["evidence"] / "series.py"
    path.write_text("FROZEN = False\n")
    with pytest.raises(ValueError, match="pinned artifact"):
        prepare(parent)
    assert not parent["output"].exists()


def test_checkpoint_checksum_and_expected_cursor_are_both_required(parent):
    parent["verification"]["content"]["branches"]["trainable"]["state"]["steps"] = 8
    with pytest.raises(ValueError, match="verified step seven"):
        prepare(parent)
    assert not parent["output"].exists()


def test_parent_archive_is_never_used_as_output_directory(parent):
    with pytest.raises(ValueError, match="independent"):
        handoff.prepare(parent["path"], output=parent["path"] / "new", **parent["options"])
    assert parent["calls"] == []


def test_failed_archive_verification_creates_no_handoff(parent, monkeypatch):
    def failed(*args, **kwargs):
        raise ValueError("failed active attempt remains quarantined")

    monkeypatch.setattr(handoff, "verify_archive", failed)
    with pytest.raises(ValueError, match="quarantined"):
        prepare(parent)
    assert not parent["output"].exists()


@pytest.mark.parametrize("name", ("../escape.py", "/absolute.py", "./extra.py", "duplicate"))
def test_resealed_unsafe_payload_is_rejected(parent, name):
    evidence = parent["evidence"]
    with tarfile.open(evidence / "payload.tar.gz") as archive:
        entries = [(member.name, archive.extractfile(member).read()) for member in archive.getmembers()]
    entries.append(entries[0] if name == "duplicate" else (name, b"pass\n"))
    data = package(entries)
    (evidence / "payload.tar.gz").write_bytes(data)
    launch = handoff.read(parent["path"] / "launch.json")
    launch["payload_sha256"] = handoff.digest(data)
    (parent["path"] / "launch.json").write_text(json.dumps(launch))
    parent["options"]["launch_sha"] = handoff.sha(parent["path"] / "launch.json")
    with pytest.raises(ValueError, match="member"):
        prepare(parent)
    assert not parent["output"].exists()
