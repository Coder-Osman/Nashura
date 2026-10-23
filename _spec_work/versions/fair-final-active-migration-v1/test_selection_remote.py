import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("selection_remote_runtime", ROOT / "selection_remote_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
spec = importlib.util.spec_from_file_location("selection_ssh", ROOT / "selection_ssh.py")
ssh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssh)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def config(tmp_path):
    base = tmp_path / "base"
    data = tmp_path / "data"
    base.mkdir()
    data.mkdir()
    source = tmp_path / "source.pt"
    source.write_bytes(b"source checkpoint")
    value = dict(schema=runtime.SCHEMA, attempt_id="selection-abc123",
        remote_root=str(base / "selection-abc123"), remote_base=str(base), python="/opt/python/bin/python",
        source=str(source), source_sha256=digest(source), data_root=str(data), gpu_index=1,
        gpu_uuid=None, timeout_seconds=1800, handoff_sha256="a" * 64,
        worker_sha256="b" * 64, runtime_sha256="c" * 64,
        uploaded_files={"inputs/selection-handoff.json": "d" * 64})
    return value


def fake_run(outputs, *, expected_environment=None):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        output = outputs(command)
        if expected_environment is not None and command[0] == "/opt/python/bin/python":
            assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "GPU-1"
            assert kwargs["env"]["PYTHONNOUSERSITE"] == "1"
        return SimpleNamespace(stdout=output)

    return run, calls


def test_config_requires_normalized_attempt_paths_and_digests(config):
    runtime.validate_config(config)
    broken = dict(config, remote_root=config["remote_base"] + "/other")
    with pytest.raises(ValueError, match="attempt root"):
        runtime.validate_config(broken)
    broken = dict(config, timeout_seconds=True)
    with pytest.raises(ValueError, match="budget"):
        runtime.validate_config(broken)


def test_resource_preflight_checks_memory_disk_source_gpu_and_cuda(config, tmp_path, monkeypatch):
    meminfo = tmp_path / "meminfo"
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    meminfo.write_text("MemAvailable:       67108864 kB\n")
    outputs = lambda command: (
        "1, GPU-1, Test GPU, 16" if "--query-gpu=" in command[1]
        else "" if "--query-compute-apps=" in command[1]
        else "Test GPU" if command[0] == config["python"]
        else "")
    run, calls = fake_run(outputs, expected_environment=True)
    monkeypatch.setattr(runtime, "gpu_inventory", lambda **kwargs:
        [dict(index=1, uuid="GPU-1", name="Test GPU", memory_used_mib=16)])
    monkeypatch.setattr(runtime, "gpu_compute_processes", lambda **kwargs: [])
    result = runtime.resource_preflight(config, run=run, meminfo_path=meminfo, proc_root=proc_root)
    assert result["status"] == "passed_preflight_only"
    assert result["gpu"]["uuid"] == "GPU-1"
    assert any(command[0] == config["python"] for command, _ in calls)


@pytest.mark.parametrize("damage,message", [
    ("memory", "memory"), ("source", "source checkpoint"), ("gpu_memory", "1 GiB"),
    ("gpu_process", "compute processes"), ("gpu_uuid", "UUID changed"), ("cuda", "exited"),
])
def test_resource_preflight_rejects_unqualified_machine(config, tmp_path, monkeypatch, damage, message):
    meminfo = tmp_path / "meminfo"
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    meminfo.write_text("MemAvailable:       1048576 kB\n" if damage == "memory"
        else "MemAvailable:       67108864 kB\n")
    if damage == "source":
        Path(config["source"]).write_bytes(b"changed")
    if damage == "gpu_uuid":
        config["gpu_uuid"] = "GPU-1"
    memory = 2048 if damage == "gpu_memory" else 16
    monkeypatch.setattr(runtime, "gpu_inventory", lambda **kwargs:
        [dict(index=1, uuid="GPU-2" if damage == "gpu_uuid" else "GPU-1",
            name="Test GPU", memory_used_mib=memory)])
    monkeypatch.setattr(runtime, "gpu_compute_processes", lambda **kwargs:
        [dict(pid=987, gpu_uuid="GPU-1")] if damage == "gpu_process" else [])
    run, _ = fake_run(lambda command: "" if command[0] == "nvidia-smi" else
        (_ for _ in ()).throw(subprocess.CalledProcessError(1, command)) if damage == "cuda" else "Test GPU")
    expected = "returned non-zero" if damage == "cuda" else message
    with pytest.raises((RuntimeError, subprocess.CalledProcessError), match=expected):
        runtime.resource_preflight(config, run=run, meminfo_path=meminfo, proc_root=proc_root)


def test_gpu_lease_is_exclusive_and_released(config):
    first, path = runtime.acquire_gpu_lock(config["remote_base"], "GPU-1")
    assert path.exists()
    with pytest.raises(RuntimeError, match="already holds"):
        runtime.acquire_gpu_lock(config["remote_base"], "GPU-1")
    os.close(first)
    second, _ = runtime.acquire_gpu_lock(config["remote_base"], "GPU-1")
    os.close(second)


def test_process_identity_checks_start_time_and_zombie(tmp_path):
    proc = tmp_path / "123"
    proc.mkdir()
    fields = ["S"] + ["0"] * 18 + ["456"]
    (proc / "stat").write_text("123 (fixture) " + " ".join(fields))
    assert runtime.process_matches(dict(pid=123, start_ticks="456"), proc_root=tmp_path)
    assert not runtime.process_matches(dict(pid=123, start_ticks="457"), proc_root=tmp_path)
    fields[0] = "Z"
    (proc / "stat").write_text("123 (fixture) " + " ".join(fields))
    assert not runtime.process_matches(dict(pid=123, start_ticks="456"), proc_root=tmp_path)


def test_inventory_distinguishes_live_failed_and_terminal(config, tmp_path, monkeypatch):
    root = Path(config["remote_root"])
    root.mkdir()
    config["gpu_uuid"] = "GPU-1"
    runtime.write_json(root / "remote-config.json", config)
    (root / "selection").mkdir()
    lease, _ = runtime.acquire_gpu_lock(config["remote_base"], "GPU-1")
    process = runtime.process_identity(os.getpid())
    monkeypatch.setattr(runtime, "process_matches", lambda identity, **kwargs: identity == process)
    runtime.write_json(root / "remote-process.json", dict(schema=runtime.SCHEMA,
        process=process, lease_base=config["remote_base"], gpu_uuid="GPU-1", status="running"))
    inventory = runtime.inventory(root)
    assert inventory["state"] == "running"
    assert "worker" in inventory["running"]
    os.close(lease)
    dead = dict(pid=99999999, start_ticks="1")
    runtime.write_json(root / "remote-process.json", dict(schema=runtime.SCHEMA,
        process=dead, lease_base=config["remote_base"], gpu_uuid="GPU-1", status="running"))
    monkeypatch.setattr(runtime, "gpu_inventory", lambda: [dict(index=1, uuid="GPU-1",
        name="Test GPU", memory_used_mib=0)])
    monkeypatch.setattr(runtime, "gpu_compute_processes", lambda: [])
    (root / "selection/completion.json").write_text(json.dumps(dict(
        status="completed_pending_independent_content_verification", process=dead)))
    inventory = runtime.inventory(root)
    assert inventory["state"] == "terminal_archive_ready_for_independent_verification"
    assert inventory["lease_available"] is True and inventory["gpu_state"]["compute_processes"] == []
    (root / "selection/completion.json").write_text(json.dumps(dict(status="wrong", process=dead)))
    assert runtime.inventory(root)["state"] == "quarantined_completion_state"


def test_upload_inventory_requires_exact_hashed_tree(config, tmp_path):
    root = Path(config["remote_root"])
    (root / "inputs").mkdir(parents=True)
    (root / "inputs/selection-handoff.json").write_bytes(b"handoff")
    (root / "run_selection.py").write_bytes(b"worker")
    (root / "selection_remote_runtime.py").write_bytes(b"runtime")
    config["uploaded_files"] = {
        "inputs/selection-handoff.json": digest(root / "inputs/selection-handoff.json"),
        "run_selection.py": digest(root / "run_selection.py"),
        "selection_remote_runtime.py": digest(root / "selection_remote_runtime.py"),
    }
    (root / "remote-config.json").write_text(json.dumps(config))
    assert set(runtime.upload_inventory(root)) == set(config["uploaded_files"])
    (root / "unexpected.bin").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="unexpected files"):
        runtime.upload_inventory(root)
def test_inventory_records_other_gpu_usage_after_owned_process_exits(config, tmp_path, monkeypatch):
    root = Path(config["remote_root"])
    root.mkdir()
    config["gpu_uuid"] = "GPU-1"
    runtime.write_json(root / "remote-config.json", config)
    (root / "selection").mkdir()
    dead = dict(pid=99999998, start_ticks="1")
    runtime.write_json(root / "remote-process.json", dict(schema=runtime.SCHEMA,
        process=dead, lease_base=config["remote_base"], gpu_uuid="GPU-1", status="running"))
    monkeypatch.setattr(runtime, "gpu_inventory", lambda: [dict(index=1, uuid="GPU-1",
        name="Test GPU", memory_used_mib=1200)])
    monkeypatch.setattr(runtime, "gpu_compute_processes", lambda: [dict(pid=42, gpu_uuid="GPU-1")])
    (root / "selection/completion.json").write_text(json.dumps(dict(
        status="completed_pending_independent_content_verification", process=dead)))
    result = runtime.inventory(root)
    assert result["state"] == "terminal_archive_ready_for_independent_verification"
    assert result["lease_available"] is True
    assert result["gpu_state"]["compute_processes"] == [dict(pid=42, gpu_uuid="GPU-1")]


def test_inventory_rejects_unpinned_gpu_lease_path(config, tmp_path, monkeypatch):
    root = Path(config["remote_root"])
    root.mkdir()
    config["gpu_uuid"] = "GPU-1"
    runtime.write_json(root / "remote-config.json", config)
    runtime.write_json(root / "remote-process.json", dict(schema=runtime.SCHEMA,
        process=dict(pid=99999997, start_ticks="1"), lease_base="/tmp/untrusted",
        gpu_uuid="GPU-1", status="running"))
    monkeypatch.setattr(runtime, "process_matches", lambda *args, **kwargs: False)
    monkeypatch.setattr(runtime, "acquire_gpu_lock", lambda *args, **kwargs:
        (_ for _ in ()).throw(AssertionError("unpinned lease path was used")))
    with pytest.raises(ValueError, match="lease differs"):
        runtime.inventory(root)


def test_inventory_rejects_symlinked_attempt_root(config, tmp_path):
    root = Path(config["remote_root"])
    root.mkdir()
    alias = tmp_path / "attempt-link"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        runtime.inventory(alias)


@pytest.mark.parametrize("name", ["remote-config.json", "remote-process.json", "selection/status.json"])
def test_inventory_rejects_symlinked_state_record(config, tmp_path, monkeypatch, name):
    root = Path(config["remote_root"])
    root.mkdir()
    config["gpu_uuid"] = "GPU-1"
    runtime.write_json(root / "remote-config.json", config)
    target = tmp_path / "state-target"
    target.write_text("{}")
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if name == "remote-config.json":
        path.unlink()
        path.symlink_to(target)
    else:
        path.symlink_to(target)
    monkeypatch.setattr(runtime, "process_matches", lambda *args, **kwargs: False)
    with pytest.raises(ValueError, match="symlink"):
        runtime.inventory(root)


def test_validate_launch_rejects_symlinked_attempt(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "launch.json").write_text("{}")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ssh.validate_launch(alias, digest(real / "launch.json"))


def test_ssh_rejects_unpinned_host_keys(monkeypatch, tmp_path):
    seen = {}

    class FakeClient:
        def connect(self, *args, **kwargs):
            seen["connect"] = (args, kwargs)

        def load_system_host_keys(self):
            seen["system"] = True

        def load_host_keys(self, path):
            seen["host_file"] = path

        def set_missing_host_key_policy(self, policy):
            seen["policy"] = policy

    monkeypatch.setattr(ssh.paramiko, "SSHClient", FakeClient)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("host ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKE\n")
    client = ssh.ssh_client("host", "user", known_hosts=str(known_hosts))
    assert isinstance(seen["policy"], ssh.paramiko.RejectPolicy)
    assert seen["host_file"] == str(known_hosts)
    assert seen["system"] is True
    assert isinstance(client, FakeClient)


def test_private_attempt_root_excludes_public_repository_and_symlinks(tmp_path):
    safe = ssh.local_private_root(tmp_path / "private")
    assert safe.stat().st_mode & 0o777 == 0o700
    public = ssh.PUBLIC_REPOSITORY / "private-test"
    with pytest.raises(ValueError, match="outside"):
        ssh.local_private_root(public)
    link = tmp_path / "link"
    link.symlink_to(safe)
    with pytest.raises(ValueError, match="symlink"):
        ssh.local_private_root(link)


def test_sftp_upload_is_atomic_and_restricts_permissions(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"payload")

    class FakeSftp:
        def __init__(self):
            self.files = {}
            self.permissions = {}

        def open(self, path, mode):
            from io import BytesIO

            outer = self
            class Writer(BytesIO):
                def close(self):
                    outer.files[path] = self.getvalue()
                    super().close()
            if mode == "wb":
                return Writer()
            raise AssertionError(mode)

        def chmod(self, path, mode):
            self.permissions[path] = mode

        def rename(self, source_path, target_path):
            self.files[target_path] = self.files.pop(source_path)

    sftp = FakeSftp()
    ssh.sftp_upload(sftp, source, "/remote/file")
    assert sftp.files == {"/remote/file": b"payload"}
    assert sftp.permissions["/remote/file.partial"] == 0o600


def test_ssh_launch_orchestration_is_fully_simulated(tmp_path, monkeypatch):
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    for name in ssh.INPUT_NAMES:
        (handoff / name).write_bytes(name.encode())
    checkpoint = b"parent checkpoint"
    source_sha = hashlib.sha256(checkpoint).hexdigest()
    deployment_data = b"pass\n"
    deployment_manifest = {"pkg/module.py": hashlib.sha256(deployment_data).hexdigest()}
    request = {"parent_checkpoint_sha256": source_sha}
    monkeypatch.setattr(ssh, "verify_handoff", lambda *args: (
        SimpleNamespace(), handoff, request,
        {"deployment.json": json.dumps(deployment_manifest).encode(), "pkg/module.py": deployment_data},
        deployment_manifest, source_sha))

    class FakeKey:
        def get_name(self):
            return "ssh-ed25519"

        def asbytes(self):
            return b"fixture host key"

    class FakeTransport:
        def get_remote_server_key(self):
            return FakeKey()

    class FakeSftp:
        def __init__(self):
            self.files = {}
            self.directories = []
            self.permissions = {}

        def mkdir(self, path, mode):
            self.directories.append((path, mode))

        def open(self, path, mode):
            from io import BytesIO

            outer = self
            class Writer(BytesIO):
                def close(self):
                    outer.files[path] = self.getvalue()
                    super().close()
            assert mode == "wb"
            return Writer()

        def chmod(self, path, mode):
            self.permissions[path] = mode

        def rename(self, source, target):
            self.files[target] = self.files.pop(source)

        def close(self):
            pass

    sftp = FakeSftp()

    class FakeClient:
        def open_sftp(self):
            return sftp

        def get_transport(self):
            return FakeTransport()

        def close(self):
            pass

    monkeypatch.setattr(ssh, "ssh_client", lambda *args, **kwargs: FakeClient())
    calls = []

    def execute_remote(client, command, *, timeout):
        calls.append((command, timeout))
        if "resource_preflight" in command:
            return json.dumps(dict(status="passed_preflight_only", gpu=dict(index=1, uuid="GPU-fixture")))
        return json.dumps(dict(status="started", process=dict(pid=123, start_ticks="456"),
            resources=dict(available_gib=64), training_admissible=False,
            formal_g2_pass=False, gates_closed=False))

    monkeypatch.setattr(ssh, "execute_remote", execute_remote)
    result = ssh.launch(handoff=handoff, handoff_sha256="a" * 64, host="fixture-host",
        username="fixture-user", private_root=tmp_path / "private", remote_base="/home/user/runs",
        remote_python="/opt/python", remote_source="/models/source.pt", remote_data_root="/data/root",
        gpu_index=1, timeout_seconds=1800)

    attempt = Path(result["local_attempt"])
    config = json.loads((attempt / "remote-config.json").read_bytes())
    remote_root = config["remote_root"]
    assert result["status"] == "started"
    assert len(calls) == 2
    assert {name for name in sftp.files} == {
        remote_root + "/inputs/" + name for name in ssh.INPUT_NAMES
    } | {remote_root + "/run_selection.py", remote_root + "/selection_remote_runtime.py",
        remote_root + "/remote-config.json"}
    for relative, expected_sha in config["uploaded_files"].items():
        remote_path = remote_root + "/" + relative
        assert hashlib.sha256(sftp.files[remote_path]).hexdigest() == expected_sha
    assert (attempt / "selection/deployment/pkg/module.py").read_bytes() == deployment_data
    launch_record = json.loads((attempt / "launch.json").read_bytes())
    assert launch_record["deployment_manifest"] == deployment_manifest
    assert launch_record["gpu_uuid"] == "GPU-fixture"
    assert all(result[name] is False for name in ("training_admissible", "formal_g2_pass", "gates_closed"))


def test_remote_inventory_command_binds_runtime_config_and_launch_identity(config):
    config["gpu_uuid"] = "GPU-1"
    config["config_sha256"] = "f" * 64
    command = ssh.remote_inventory_command(config, python=config["python"])
    assert "selection_remote_runtime.py" in command
    assert config["remote_root"] in command
    assert config["config_sha256"] in command
    assert config["runtime_sha256"] in command


def test_remote_attempt_local_copy_is_separate_and_complete(tmp_path):
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    for name in ssh.INPUT_NAMES:
        (handoff / name).write_bytes(name.encode())
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    payload = {"deployment.json": b"{}", "pkg/module.py": b"pass\n"}
    ssh.local_attempt_contents(attempt, handoff, {}, payload)
    assert (attempt / "selection/inputs/selection-handoff.json").read_bytes() == b"selection-handoff.json"
    assert (attempt / "selection/deployment/pkg/module.py").read_bytes() == b"pass\n"
    assert digest(attempt / "run_selection.py") == digest(ssh.WORKER)
    assert digest(attempt / "selection_remote_runtime.py") == digest(ssh.RUNTIME)


def test_remote_attempt_validation_binds_external_launch_sha(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    launch = dict(schema="FairFinalActiveSelectionRemoteLaunch/v1", attempt_id="selection-a",
        remote_base="/home/user/runs", remote_root="/home/user/runs/selection-a",
        remote_python="/opt/python", source="/home/user/source.pt", data_root="/home/user/data",
        training_admissible=False, formal_g2_pass=False, gates_closed=False)
    (attempt / "launch.json").write_text(json.dumps(launch))
    expected = digest(attempt / "launch.json")
    validated, record = ssh.validate_launch(attempt, expected)
    assert validated == attempt and record == launch
    with pytest.raises(ValueError, match="pinned"):
        ssh.validate_launch(attempt, "0" * 64)
    launch["remote_root"] = "/home/user/runs/../bad"
    (attempt / "launch.json").write_text(json.dumps(launch))
    with pytest.raises(ValueError, match="normalized"):
        ssh.validate_launch(attempt, digest(attempt / "launch.json"))


def test_archive_tree_rejects_mutated_remote_launcher_copy(tmp_path):
    attempt = tmp_path / "attempt"
    inputs = attempt / "selection/inputs"
    deployment = attempt / "selection/deployment"
    inputs.mkdir(parents=True)
    deployment.mkdir(parents=True)
    for name in ssh.INPUT_NAMES:
        (inputs / name).write_bytes(name.encode())
    (attempt / "run_selection.py").write_bytes(b"worker")
    (attempt / "selection_remote_runtime.py").write_bytes(b"runtime")
    manifest = {"pkg/code.py": hashlib.sha256(b"pass\n").hexdigest()}
    (deployment / "deployment.json").write_text(json.dumps(manifest))
    (deployment / "pkg").mkdir()
    (deployment / "pkg/code.py").write_bytes(b"pass\n")
    launch = dict(uploaded_files={"inputs/" + name: digest(inputs / name) for name in ssh.INPUT_NAMES}
        | {"run_selection.py": digest(attempt / "run_selection.py"),
            "selection_remote_runtime.py": digest(attempt / "selection_remote_runtime.py")},
        deployment_manifest=manifest)
    assert ssh.expected_remote_tree(attempt, launch)
    (attempt / "run_selection.py").write_bytes(b"modified worker")
    with pytest.raises(ValueError, match="runtime differs"):
        ssh.expected_remote_tree(attempt, launch)


def test_collector_reuses_only_verified_archive_files(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    (attempt / "selection/inputs").mkdir(parents=True)
    (attempt / "selection/deployment/pkg").mkdir(parents=True)
    for name in ssh.INPUT_NAMES:
        (attempt / "selection/inputs" / name).write_bytes(name.encode())
    code = b"pass\n"
    deployment_manifest = {"pkg/code.py": hashlib.sha256(code).hexdigest()}
    (attempt / "selection/deployment/deployment.json").write_text(json.dumps(deployment_manifest))
    (attempt / "selection/deployment/pkg/code.py").write_bytes(code)
    (attempt / "run_selection.py").write_bytes(b"worker")
    (attempt / "selection_remote_runtime.py").write_bytes(b"runtime")
    uploaded_files = {"inputs/" + name: digest(attempt / "selection/inputs" / name)
        for name in ssh.INPUT_NAMES}
    uploaded_files.update({"run_selection.py": digest(attempt / "run_selection.py"),
        "selection_remote_runtime.py": digest(attempt / "selection_remote_runtime.py")})
    launch_record = dict(schema="FairFinalActiveSelectionRemoteLaunch/v1", attempt_id="selection-a",
        host="gpu", username="user", remote_base="/home/user/runs",
        remote_root="/home/user/runs/selection-a", remote_python="/opt/python",
        source="/home/user/source.pt", data_root="/home/user/data", gpu_index=1, gpu_uuid="GPU-1",
        runtime_sha256="r" * 64, uploaded_files=uploaded_files,
        deployment_manifest=deployment_manifest,
        training_admissible=False, formal_g2_pass=False, gates_closed=False)
    config = dict(schema=runtime.SCHEMA, attempt_id="selection-a", remote_root=launch_record["remote_root"],
        remote_base=launch_record["remote_base"], python=launch_record["remote_python"],
        source=launch_record["source"], source_sha256="a" * 64, data_root=launch_record["data_root"],
        gpu_index=1, gpu_uuid="GPU-1", timeout_seconds=1800, handoff_sha256="b" * 64,
        worker_sha256="c" * 64, runtime_sha256=launch_record["runtime_sha256"],
        uploaded_files=uploaded_files)
    (attempt / "remote-config.json").write_text(json.dumps(config))
    launch_record["config_sha256"] = digest(attempt / "remote-config.json")
    (attempt / "launch.json").write_text(json.dumps(launch_record))
    launch_sha = digest(attempt / "launch.json")
    output_files = {"execution.json": b"execution", "completion.json": b"completion"}
    metadata = {name: dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
        for name, data in output_files.items()}
    handoff_metadata = ssh.expected_remote_tree(attempt, launch_record)
    remote_process = dict(schema=runtime.SCHEMA, config_sha256=launch_record["config_sha256"],
        gpu_uuid="GPU-1", status="running")
    remote_process_bytes = json.dumps(remote_process, sort_keys=True, allow_nan=False).encode()
    config_bytes = (attempt / "remote-config.json").read_bytes()
    remote_metadata = {
        "remote-config.json": dict(sha256=hashlib.sha256(config_bytes).hexdigest(), bytes=len(config_bytes)),
        "remote-process.json": dict(sha256=hashlib.sha256(remote_process_bytes).hexdigest(),
            bytes=len(remote_process_bytes)),
    }
    inventory = dict(schema=runtime.SCHEMA, state="terminal_archive_ready_for_independent_verification",
        files=handoff_metadata | metadata,
        metadata=remote_metadata, remote_process=remote_process, lease_available=True, gpu_state={})
    remote_data = {str(Path(launch_record["remote_root"]) / "selection" / name): data
        for name, data in output_files.items()}
    remote_data[str(Path(launch_record["remote_root"]) / "remote-config.json")] = config_bytes
    remote_data[str(Path(launch_record["remote_root"]) / "remote-process.json")] = remote_process_bytes

    class FakeSftp:
        def open(self, path, mode):
            from io import BytesIO
            assert mode == "rb"
            return BytesIO(remote_data[path])

        def close(self):
            pass

    class FakeClient:
        def open_sftp(self):
            return FakeSftp()

        def close(self):
            pass

    monkeypatch.setattr(ssh, "ssh_client", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(ssh, "execute_remote", lambda *args, **kwargs: json.dumps(inventory))
    process_path = str(Path(launch_record["remote_root"]) / "remote-process.json")
    remote_data[process_path] = b"tampered process record"
    with pytest.raises(ValueError, match="metadata file differs"):
        ssh.collect(attempt=attempt, launch_sha256=launch_sha)
    remote_data[process_path] = remote_process_bytes
    receipt = ssh.collect(attempt=attempt, launch_sha256=launch_sha)
    assert receipt["status"] == "terminal_archive_ready_for_independent_verification"
    assert digest(attempt / "selection/execution.json") == metadata["execution.json"]["sha256"]
    assert digest(attempt / "selection/completion.json") == metadata["completion.json"]["sha256"]
    assert ssh.collect(attempt=attempt, launch_sha256=launch_sha) == receipt
    (attempt / "selection/completion.json").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="previously collected"):
        ssh.collect(attempt=attempt, launch_sha256=launch_sha)


def test_collector_does_not_download_while_remote_process_or_gpu_is_live(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    launch_record = dict(schema="FairFinalActiveSelectionRemoteLaunch/v1", attempt_id="selection-a",
        host="gpu", username="user", remote_base="/home/user/runs",
        remote_root="/home/user/runs/selection-a", remote_python="/opt/python",
        gpu_index=1, gpu_uuid="GPU-1",
        runtime_sha256="r" * 64,
        source="/home/user/source.pt", data_root="/home/user/data",
        training_admissible=False, formal_g2_pass=False, gates_closed=False)
    uploaded_files = {"inputs/selection-handoff.json": "d" * 64}
    config = dict(schema=runtime.SCHEMA, attempt_id="selection-a", remote_root=launch_record["remote_root"],
        remote_base=launch_record["remote_base"], python=launch_record["remote_python"],
        source=launch_record["source"], source_sha256="a" * 64, data_root=launch_record["data_root"],
        gpu_index=1, gpu_uuid="GPU-1", timeout_seconds=1800, handoff_sha256="b" * 64,
        worker_sha256="c" * 64, runtime_sha256=launch_record["runtime_sha256"],
        uploaded_files=uploaded_files)
    (attempt / "remote-config.json").write_text(json.dumps(config))
    launch_record["config_sha256"] = digest(attempt / "remote-config.json")
    (attempt / "launch.json").write_text(json.dumps(launch_record))
    launch_sha = digest(attempt / "launch.json")
    monkeypatch.setattr(ssh, "ssh_client", lambda *args, **kwargs: SimpleNamespace(close=lambda: None))
    config_bytes = (attempt / "remote-config.json").read_bytes()
    live_process = dict(schema=runtime.SCHEMA, config_sha256=launch_record["config_sha256"],
        gpu_uuid="GPU-1", status="running")
    process_bytes = json.dumps(live_process, sort_keys=True, allow_nan=False).encode()
    for state in ("running", "orphaned_process_live"):
        inventory = dict(schema=runtime.SCHEMA, state=state, files={}, running=["worker"],
            metadata={"remote-config.json": dict(sha256=launch_record["config_sha256"],
                    bytes=len(config_bytes)),
                "remote-process.json": dict(sha256=hashlib.sha256(process_bytes).hexdigest(),
                    bytes=len(process_bytes))},
            remote_process=live_process)
        monkeypatch.setattr(ssh, "execute_remote", lambda *args, value=inventory, **kwargs: json.dumps(value))
        result = ssh.collect(attempt=attempt, launch_sha256=launch_sha)
        assert result["status"] == state
        assert not (attempt / "native-download-receipt.json").exists()
