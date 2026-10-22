import hashlib
import io
import json
import tarfile

import pytest

from collect_native import publish_archive


def fixture(tmp_path):
    contents = {"supervisor.py": b"frozen", "completion.json": b'{"exit_code":124}'}
    launch = dict(remote="/fixture", source_sha256={"supervisor.py": hashlib.sha256(b"frozen").hexdigest()})
    (tmp_path / "launch.json").write_text(json.dumps(launch))
    inventory = dict(status="terminated", completion=dict(exit_code=124), files={
        name: dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest()) for name, data in contents.items()})
    return launch, inventory, contents


def test_failed_training_archives_without_pass_and_preserves_receipt(tmp_path):
    launch, inventory, contents = fixture(tmp_path)
    result = publish_archive(tmp_path, launch, inventory, contents.__getitem__)
    assert result["completion"]["exit_code"] == 124
    assert result["native_resume_verified"] is False
    receipt = (tmp_path / "native-download-receipt.json").read_bytes()
    publish_archive(tmp_path, launch, inventory, lambda name: pytest.fail("download repeated"))
    assert receipt == (tmp_path / "native-download-receipt.json").read_bytes()
    inventory["files"]["completion.json"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="pinned native archive"):
        publish_archive(tmp_path, launch, inventory, contents.__getitem__)
    assert receipt == (tmp_path / "native-download-receipt.json").read_bytes()


@pytest.mark.parametrize("kind", ["deployment", "transfer", "escape"])
def test_bad_archive_has_no_receipt(tmp_path, kind):
    launch, inventory, contents = fixture(tmp_path)
    if kind == "deployment":
        launch["source_sha256"]["supervisor.py"] = "0" * 64
    elif kind == "transfer":
        contents["completion.json"] = b"changed"
    else:
        inventory["files"]["../escape"] = dict(bytes=0, sha256=hashlib.sha256(b"").hexdigest())
    with pytest.raises(ValueError):
        publish_archive(tmp_path, launch, inventory, contents.__getitem__)
    assert not (tmp_path / "native-download-receipt.json").exists()


def test_live_attempt_is_not_archived(tmp_path):
    report = publish_archive(tmp_path, {}, dict(status="running"), lambda name: pytest.fail(name))
    assert report["status"] == "running"
    assert not list(tmp_path.iterdir())


def test_resume_archive_anchors_deployment_to_uploaded_parent_package(tmp_path):
    launch, inventory, contents = fixture(tmp_path)
    manifest = json.dumps(launch.pop("source_sha256")).encode()
    package = io.BytesIO()
    with tarfile.open(fileobj=package, mode="w:gz") as archive:
        member = tarfile.TarInfo("deployment.json")
        member.size = len(manifest)
        archive.addfile(member, io.BytesIO(manifest))
    contents.update({"payload.tar.gz": package.getvalue(), "deployment.json": manifest})
    launch["uploaded_sha256"] = {"payload.tar.gz": hashlib.sha256(package.getvalue()).hexdigest()}
    for name, data in contents.items():
        inventory["files"][name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    valid = publish_archive(tmp_path, launch, inventory, contents.__getitem__)
    assert valid["deployment_verified"] is True
    receipt = (tmp_path / "native-download-receipt.json").read_bytes()
    contents["supervisor.py"] = b"modified source"
    inventory["files"]["supervisor.py"] = dict(bytes=15, sha256=hashlib.sha256(contents["supervisor.py"]).hexdigest())
    with pytest.raises(ValueError, match="frozen deployment differs"):
        publish_archive(tmp_path, launch, inventory, contents.__getitem__)
    assert receipt == (tmp_path / "native-download-receipt.json").read_bytes()
