import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("active_payload_builder", ROOT / "build_payload.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def package(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in files:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


@pytest.fixture
def parent(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    version = workspace / "_spec_work/versions/fair-final-active-migration-v1"
    version.mkdir(parents=True)
    (version / "probe_active_resume.py").write_text("MODE = 'resume'\n")
    batch = version.parent / "fair-final-migration-batch-v1"
    batch.mkdir()
    (batch / "probe_native_batch.py").write_text(
        "def benchmark(students, teacher, plan, args, frozen):\n"
        "        students[0].copy_encoder(teacher)\n")
    files = {name: b"VERSION = 'scalar'\n" for name in builder.ACTIVE}
    files[builder.MIGRATION] = (
        b"from .fair_final.adapter import source_policy\n"
        b"from .fair_final.learning import conditional_teacher_kl\n")
    files[builder.SERIES] = (
        b"def implementation_sha():\n"
        b"    return semantic_digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})\n")
    identity = {name: builder.digest(data) for name, data in files.items()}
    files["deployment.json"] = json.dumps(identity).encode()
    directory = tmp_path / "parent"
    directory.mkdir()

    def publish(entries=None):
        data = package(list(files.items()) if entries is None else entries)
        (directory / "payload.tar.gz").write_bytes(data)
        (directory / "launch.json").write_text(json.dumps(dict(
            payload_sha256=builder.digest(data), source_sha256=identity)))

    publish()
    for name in builder.ACTIVE:
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("VERSION = 'active'\n")
    monkeypatch.setattr(builder, "WORKSPACE", workspace)
    monkeypatch.setattr(builder, "ROOT", version)
    return directory, files, publish


def test_active_sources_scalar_reference_and_complete_identity_are_independent(parent):
    directory, original, _ = parent
    data, identity, changes = builder.build(directory)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
    assert json.loads(files["deployment.json"]) == identity
    assert identity == {name: builder.digest(value) for name, value in files.items()
        if name != "deployment.json"}
    for name in builder.ACTIVE:
        assert files[name] == (builder.WORKSPACE / name).read_bytes()
        assert changes[name] == dict(before=builder.digest(original[name]), after=identity[name])
    for name in ("adapter", "learning"):
        prefix = "firstlight_CR/native_runner/training/fair_final/"
        assert files[prefix + "reference_" + name + ".py"] == original[prefix + name + ".py"]
    assert b"fair_final.reference_adapter" in files["reference_migration.py"]
    assert b"fair_final.reference_learning" in files["reference_migration.py"]
    assert b"active={name:" in files[builder.SERIES]
    assert b"copy_encoder_with_audit" in files["probe_active_batch.py"]


def test_parent_package_and_deployment_tampering_are_rejected(parent):
    directory, files, publish = parent
    (directory / "payload.tar.gz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="payload checksum"):
        builder.build(directory)
    files[builder.MIGRATION] += b"CHANGED = True\n"
    publish()
    with pytest.raises(ValueError, match="deployment"):
        builder.build(directory)


@pytest.mark.parametrize("name", ("../escape.py", "/absolute.py", "a/../escape.py", "./extra.py"))
def test_noncanonical_archive_paths_are_rejected(parent, name):
    directory, files, publish = parent
    publish([*files.items(), (name, b"pass\n")])
    with pytest.raises(ValueError, match="member"):
        builder.build(directory)


def test_duplicate_archive_member_is_rejected(parent):
    directory, files, publish = parent
    publish([*files.items(), next(iter(files.items()))])
    with pytest.raises(ValueError, match="member"):
        builder.build(directory)


def test_changed_parent_anchor_is_rejected_instead_of_silently_reused(parent):
    directory, files, publish = parent
    files[builder.SERIES] = b"def implementation_sha():\n    return 'old'\n"
    identity = {name: builder.digest(data) for name, data in files.items() if name != "deployment.json"}
    files["deployment.json"] = json.dumps(identity).encode()
    publish()
    launch = json.loads((directory / "launch.json").read_text())
    launch["source_sha256"] = identity
    (directory / "launch.json").write_text(json.dumps(launch))
    with pytest.raises(ValueError, match="anchor"):
        builder.build(directory)
