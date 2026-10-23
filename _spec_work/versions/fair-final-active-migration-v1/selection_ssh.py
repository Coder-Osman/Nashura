import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import sys
import time

import paramiko


ROOT = Path(__file__).resolve().parent
PUBLIC_REPOSITORY = ROOT.parents[3]
RUNTIME = ROOT / "selection_remote_runtime.py"
WORKER = ROOT / "run_selection.py"
INPUT_NAMES = ("payload.tar.gz", "parent-checkpoint.pt", "parent-verification.json",
    "selection-handoff.json")


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_bytes())


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("xb") as stream:
        stream.write(json.dumps(value, sort_keys=True, allow_nan=False).encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def absolute_remote_path(value, *, name):
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value or str(path) != value:
        raise ValueError(name + " must be a normalized absolute remote path")
    return str(path)


def local_private_root(value):
    supplied = Path(value).expanduser()
    if supplied.is_symlink():
        raise ValueError("private attempts root cannot be a symlink")
    root = supplied.resolve()
    if root == PUBLIC_REPOSITORY or root.is_relative_to(PUBLIC_REPOSITORY):
        raise ValueError("private attempts must be outside the public repository")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValueError("private attempt root must be a real directory")
    os.chmod(root, 0o700)
    return root


def ssh_client(host, username, *, password=None, known_hosts=None):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    host_file = Path(known_hosts).expanduser() if known_hosts else Path.home() / ".ssh/known_hosts"
    if host_file.is_file():
        client.load_host_keys(str(host_file))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(host, username=username, password=password, timeout=15, auth_timeout=15,
        banner_timeout=15, allow_agent=True, look_for_keys=True)
    return client


def execute_remote(client, command, *, timeout=120):
    _, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
    output = stdout.read().decode("utf-8", errors="replace")
    errors = stderr.read().decode("utf-8", errors="replace")
    status = stdout.channel.recv_exit_status()
    if status:
        raise RuntimeError("remote command failed with status " + str(status) + ": " + errors[-2000:])
    return output


def import_worker_helpers():
    spec = importlib.util.spec_from_file_location("nashura_selection_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def import_remote_runtime():
    spec = importlib.util.spec_from_file_location("nashura_selection_remote_runtime", RUNTIME)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preflight_command(config, *, python):
    source = base64.b64encode(RUNTIME.read_bytes()).decode("ascii")
    encoded_config = base64.b64encode(json.dumps(config, sort_keys=True).encode()).decode("ascii")
    program = (
        "import base64,json,types; module=types.ModuleType('selection_remote_runtime'); "
        "exec(compile(base64.b64decode(" + repr(source) + "),'selection_remote_runtime.py','exec'),module.__dict__); "
        "config=json.loads(base64.b64decode(" + repr(encoded_config) + ")); "
        "print(json.dumps(module.resource_preflight(config),sort_keys=True))"
    )
    return shlex.join([python, "-B", "-c", program])


def remote_inventory_command(config, *, python):
    runtime_path = str(PurePosixPath(config["remote_root"]) / "selection_remote_runtime.py")
    config_path = str(PurePosixPath(config["remote_root"]) / "remote-config.json")
    runtime_sha = config["runtime_sha256"]
    config_sha = config["config_sha256"]
    root = config["remote_root"]
    program = (
        "import hashlib,json,runpy,sys; runtime=" + repr(runtime_path) + "; config=" + repr(config_path) + "; "
        "digest=lambda p:hashlib.sha256(open(p,'rb').read()).hexdigest(); "
        "assert digest(runtime)==" + repr(runtime_sha) + "; assert digest(config)==" + repr(config_sha) + "; "
        "sys.argv=[runtime,'inventory','--root'," + repr(root) + "]; runpy.run_path(runtime,run_name='__main__')"
    )
    return shlex.join([python, "-B", "-c", program])


def verify_handoff(handoff, expected_sha):
    worker = import_worker_helpers()
    handoff = Path(handoff).resolve(strict=True)
    request = worker.checked_handoff(handoff, expected_sha)
    files, manifest = worker.payload_files(handoff, request)
    for name in INPUT_NAMES:
        path = handoff / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("selection handoff input is not a regular file")
    source_sha = request["dataset_identity"]["source_a_sha"]
    return worker, handoff, request, files, manifest, source_sha


def make_config(*, attempt_id, remote_root, remote_base, python, source, source_sha,
        data_root, gpu_index, gpu_uuid, timeout, handoff_sha, uploaded_files):
    runtime = import_remote_runtime()
    config = dict(schema=runtime.SCHEMA, attempt_id=attempt_id, remote_root=remote_root,
        remote_base=remote_base, python=python, source=source, source_sha256=source_sha,
        data_root=data_root, gpu_index=gpu_index, gpu_uuid=gpu_uuid,
        timeout_seconds=timeout, handoff_sha256=handoff_sha, worker_sha256=sha(WORKER),
        runtime_sha256=sha(RUNTIME), uploaded_files=uploaded_files)
    runtime.validate_config(config)
    return config


def local_attempt_contents(attempt, handoff, request, payload_files):
    selection = attempt / "selection"
    input_dir = selection / "inputs"
    deployment = selection / "deployment"
    input_dir.mkdir(parents=True, mode=0o700)
    deployment.mkdir(mode=0o700)
    for name in INPUT_NAMES:
        shutil.copyfile(handoff / name, input_dir / name)
    for name, data in payload_files.items():
        path = deployment / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(data)
    shutil.copyfile(WORKER, attempt / "run_selection.py")
    shutil.copyfile(RUNTIME, attempt / "selection_remote_runtime.py")


def sftp_upload(sftp, local_path, remote_path):
    local_path, remote_path = Path(local_path), str(remote_path)
    temporary = remote_path + ".partial"
    with local_path.open("rb") as source, sftp.open(temporary, "wb") as destination:
        shutil.copyfileobj(source, destination, length=1024 * 1024)
    sftp.chmod(temporary, 0o600)
    sftp.rename(temporary, remote_path)


def launch(*, handoff, handoff_sha256, host, username, private_root, remote_base,
        remote_python, remote_source, remote_data_root, gpu_index=1, timeout_seconds=28800,
        password=None, known_hosts=None):
    worker, handoff, request, payload_files, deployment_manifest, source_sha = verify_handoff(
        handoff, handoff_sha256)
    remote_base = absolute_remote_path(remote_base, name="remote base")
    remote_source = absolute_remote_path(remote_source, name="remote source")
    remote_data_root = absolute_remote_path(remote_data_root, name="remote data root")
    remote_python = absolute_remote_path(remote_python, name="remote Python")
    if not 1800 <= timeout_seconds <= 28800 or not 0 <= gpu_index < 8:
        raise ValueError("bounded timeout or physical GPU index differs")
    if remote_base in ("/", "/home", "/tmp"):
        raise ValueError("remote base must be a user-owned experiment directory")
    attempt_id = "selection-" + str(time.time_ns())
    remote_root = str(PurePosixPath(remote_base) / attempt_id)
    uploaded = {"inputs/" + name: sha(handoff / name) for name in INPUT_NAMES}
    uploaded["run_selection.py"] = sha(WORKER)
    uploaded["selection_remote_runtime.py"] = sha(RUNTIME)
    config = make_config(attempt_id=attempt_id, remote_root=remote_root, remote_base=remote_base,
        python=remote_python, source=remote_source, source_sha=source_sha, data_root=remote_data_root,
        gpu_index=gpu_index, gpu_uuid=None, timeout=timeout_seconds,
        handoff_sha=handoff_sha256, uploaded_files=uploaded)
    client = ssh_client(host, username, password=password, known_hosts=known_hosts)
    local_attempt = None
    try:
        preflight_raw = execute_remote(client, preflight_command(config, python=remote_python), timeout=180)
        preflight = json.loads(preflight_raw)
        if preflight.get("status") != "passed_preflight_only" or preflight["gpu"]["index"] != gpu_index:
            raise ValueError("remote resource preflight did not qualify")
        host_key = client.get_transport().get_remote_server_key()
        host_key_identity = dict(key_type=host_key.get_name(),
            sha256=hashlib.sha256(host_key.asbytes()).hexdigest())
        config["gpu_uuid"] = preflight["gpu"]["uuid"]
        import_remote_runtime().validate_config(config)
        private = local_private_root(private_root)
        local_attempt = private / attempt_id
        local_attempt.mkdir(mode=0o700, exist_ok=False)
        local_attempt_contents(local_attempt, handoff, request, payload_files)
        config_path = local_attempt / "remote-config.json"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2))
        config_sha = sha(config_path)
        launch_record = dict(schema="FairFinalActiveSelectionRemoteLaunch/v1", attempt_id=attempt_id,
            host=host, username=username, remote_base=remote_base, remote_root=remote_root,
            remote_python=remote_python, source=remote_source, data_root=remote_data_root,
            source_sha256=source_sha, handoff_sha256=handoff_sha256,
            parent_checkpoint_sha256=request["parent_checkpoint_sha256"], gpu_index=gpu_index,
            gpu_uuid=config["gpu_uuid"], timeout_seconds=timeout_seconds,
            host_key=host_key_identity,
            preflight=preflight, uploaded_files=uploaded, deployment_manifest=deployment_manifest,
            runtime_sha256=config["runtime_sha256"], worker_sha256=config["worker_sha256"],
            config_sha256=config_sha, training_admissible=False, formal_g2_pass=False,
            gates_closed=False)
        write_json(local_attempt / "launch.json", launch_record)
        launch_sha = sha(local_attempt / "launch.json")
        sftp = client.open_sftp()
        try:
            sftp.mkdir(remote_root, mode=0o700)
            sftp.mkdir(remote_root + "/inputs", mode=0o700)
            for name in INPUT_NAMES:
                sftp_upload(sftp, handoff / name, remote_root + "/inputs/" + name)
            sftp_upload(sftp, WORKER, remote_root + "/run_selection.py")
            sftp_upload(sftp, RUNTIME, remote_root + "/selection_remote_runtime.py")
            sftp_upload(sftp, config_path, remote_root + "/remote-config.json")
        finally:
            sftp.close()
        spawn = remote_root + "/selection_remote_runtime.py"
        command = shlex.join([remote_python, "-B", spawn, "spawn", "--root", remote_root,
            "--config-sha", config_sha])
        result = json.loads(execute_remote(client, command, timeout=180))
        write_json(local_attempt / "launch-result.json", dict(status=result["status"],
            launch_sha256=launch_sha, remote_process=result.get("process"),
            resources=result.get("resources"), **{name: False for name in
                ("training_admissible", "formal_g2_pass", "gates_closed")}))
        return dict(local_attempt=str(local_attempt), attempt_id=attempt_id,
            launch_sha256=launch_sha, remote_process=result.get("process"),
            status=result["status"], resources=result.get("resources"),
            training_admissible=False, formal_g2_pass=False, gates_closed=False)
    except BaseException as error:
        if local_attempt is not None:
            failure = dict(status="launch_failed_or_remote_outcome_unknown",
                error=type(error).__name__, attempt_id=attempt_id,
                remote_root=remote_root, training_admissible=False,
                formal_g2_pass=False, gates_closed=False)
            failure_path = local_attempt / "launch-failure.json"
            if not failure_path.exists():
                write_json(failure_path, failure)
        raise
    finally:
        client.close()


def validate_launch(attempt, expected_sha):
    supplied_attempt = Path(attempt)
    if supplied_attempt.is_symlink():
        raise ValueError("private attempt cannot be a symlink")
    attempt = supplied_attempt.resolve(strict=True)
    if attempt.is_relative_to(PUBLIC_REPOSITORY):
        raise ValueError("private native archive is inside public repository")
    launch_path = attempt / "launch.json"
    if launch_path.is_symlink() or not launch_path.is_file():
        raise ValueError("remote launch record must be a regular file")
    if sha(launch_path) != expected_sha:
        raise ValueError("pinned remote launch record differs")
    launch_record = read_json(launch_path)
    if (launch_record.get("schema") != "FairFinalActiveSelectionRemoteLaunch/v1"
            or any(launch_record.get(name) is not False for name in
                ("training_admissible", "formal_g2_pass", "gates_closed"))):
        raise ValueError("remote launch record has invalid schema or scope")
    for field in ("remote_base", "remote_root", "remote_python", "source", "data_root"):
        absolute_remote_path(launch_record[field], name=field)
    attempt_id = launch_record.get("attempt_id")
    if (not isinstance(attempt_id, str) or not attempt_id.startswith("selection-")
            or PurePosixPath(attempt_id).name != attempt_id
            or PurePosixPath(launch_record["remote_root"]) != PurePosixPath(launch_record["remote_base"]) / attempt_id):
        raise ValueError("remote attempt is outside configured base")
    return attempt, launch_record


def safe_relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("remote artifact path is unsafe")
    return path


def regular_file_tree(root):
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("local selection mirror contains symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("local selection mirror contains non-regular file")
        files[path.relative_to(root).as_posix()] = path
    return files


def expected_remote_tree(attempt, launch_record):
    expected = {}
    selection = Path(attempt) / "selection"
    input_root = selection / "inputs"
    deployment_root = selection / "deployment"
    if any(path.is_symlink() or not path.is_dir() for path in (input_root, deployment_root)):
        raise ValueError("local selection mirror roots are absent or symlinked")
    uploaded = launch_record.get("uploaded_files")
    if not isinstance(uploaded, dict):
        raise ValueError("launch record omits its uploaded-file manifest")
    expected_inputs = {name.removeprefix("inputs/"): digest for name, digest in uploaded.items()
        if name.startswith("inputs/")}
    if (set(expected_inputs) != set(INPUT_NAMES)
            or set(uploaded) != {"inputs/" + name for name in INPUT_NAMES}
                | {"run_selection.py", "selection_remote_runtime.py"}):
        raise ValueError("launch record input manifest is incomplete")
    input_files = regular_file_tree(input_root)
    if set(input_files) != set(expected_inputs):
        raise ValueError("local input mirror has missing or unexpected files")
    for name, expected_sha in expected_inputs.items():
        path = input_files[name]
        if sha(path) != expected_sha:
            raise ValueError("local input mirror differs from launch manifest: " + name)
        expected["inputs/" + name] = dict(sha256=expected_sha, bytes=path.stat().st_size)
    for name in ("run_selection.py", "selection_remote_runtime.py"):
        path = Path(attempt) / name
        if path.is_symlink() or not path.is_file() or sha(path) != uploaded[name]:
            raise ValueError("local uploaded runtime differs from launch manifest: " + name)

    manifest = launch_record.get("deployment_manifest")
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("launch record deployment manifest is absent")
    manifest_path = deployment_root / "deployment.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or json.loads(manifest_path.read_bytes()) != manifest:
        raise ValueError("local deployment manifest differs from launch record")
    deployment_files = regular_file_tree(deployment_root)
    if set(deployment_files) != set(manifest) | {"deployment.json"}:
        raise ValueError("local deployment tree is incomplete or contains unexpected files")
    for name, expected_sha in manifest.items():
        path = deployment_files[name]
        safe_relative(name)
        if (not isinstance(expected_sha, str) or len(expected_sha) != 64
                or any(char not in "0123456789abcdef" for char in expected_sha)
                or sha(path) != expected_sha):
            raise ValueError("local deployment file differs from launch manifest: " + name)
        expected["deployment/" + name] = dict(sha256=expected_sha, bytes=path.stat().st_size)
    expected["deployment/deployment.json"] = dict(sha256=sha(manifest_path),
        bytes=manifest_path.stat().st_size)
    return expected


def verify_remote_mirror(files, expected):
    if not isinstance(files, dict):
        raise ValueError("remote artifact inventory is malformed")
    mirror = {}
    for name, metadata in files.items():
        path = safe_relative(name)
        if (not isinstance(metadata, dict) or set(metadata) != {"sha256", "bytes"}
                or not isinstance(metadata["sha256"], str) or len(metadata["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in metadata["sha256"])
                or type(metadata["bytes"]) is not int or metadata["bytes"] < 0):
            raise ValueError("remote artifact metadata is malformed")
        if path.parts[0] in ("inputs", "deployment"):
            mirror[name] = metadata
    if mirror != expected:
        raise ValueError("remote frozen input or deployment tree differs")


def validate_remote_metadata(attempt, launch_record, inventory):
    metadata = inventory.get("metadata", {})
    if not isinstance(metadata, dict) or set(metadata) - {
            "remote-config.json", "remote-process.json", "remote-launch-failure.json"}:
        raise ValueError("remote metadata inventory is malformed")
    recorded = metadata.get("remote-config.json")
    config_path = Path(attempt) / "remote-config.json"
    if (not config_path.is_file() or config_path.is_symlink()
            or not isinstance(recorded, dict) or recorded.get("sha256") != launch_record["config_sha256"]
            or type(recorded.get("bytes")) is not int or recorded["bytes"] != config_path.stat().st_size
            or sha(config_path) != launch_record["config_sha256"]):
        raise ValueError("remote configuration receipt differs")
    process_record = inventory.get("remote_process")
    process_file = metadata.get("remote-process.json")
    if process_record is not None:
        if (not isinstance(process_file, dict) or set(process_file) != {"sha256", "bytes"}
                or not isinstance(process_file.get("sha256"), str)
                or len(process_file["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in process_file["sha256"])
                or type(process_file.get("bytes")) is not int or process_file["bytes"] < 1):
            raise ValueError("remote process receipt is malformed")
        if (process_record.get("schema") != "FairFinalActiveSelectionRemote/v1"
                or process_record.get("config_sha256") != launch_record["config_sha256"]
                or process_record.get("gpu_uuid") != launch_record["gpu_uuid"]):
            raise ValueError("remote process identity differs from launch")
    elif process_file is not None:
        raise ValueError("remote process metadata has no matching process record")
    if inventory.get("state") == "terminal_archive_ready_for_independent_verification" and process_record is None:
        raise ValueError("terminal remote archive omits its process record")
    for name, details in metadata.items():
        if (not isinstance(details, dict) or set(details) != {"sha256", "bytes"}
                or not isinstance(details.get("sha256"), str) or len(details["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in details["sha256"])
                or type(details.get("bytes")) is not int or details["bytes"] < 1
                or details["bytes"] > 1024 * 1024):
            raise ValueError("remote metadata file descriptor is malformed: " + name)


def verify_remote_metadata_files(sftp, attempt, launch_record, inventory):
    verified = {}
    for name, details in inventory["metadata"].items():
        remote_path = str(PurePosixPath(launch_record["remote_root"]) / name)
        content = bytearray()
        digest = hashlib.sha256()
        size = 0
        with sftp.open(remote_path, "rb") as source:
            while True:
                block = source.read(64 * 1024)
                if not block:
                    break
                content.extend(block)
                digest.update(block)
                size += len(block)
        if digest.hexdigest() != details["sha256"] or size != details["bytes"]:
            raise ValueError("remote metadata file differs from its inventory: " + name)
        if name == "remote-config.json":
            if bytes(content) != (Path(attempt) / name).read_bytes():
                raise ValueError("downloaded remote configuration differs from pinned local copy")
        elif name == "remote-process.json":
            try:
                process_record = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("remote process metadata is not valid JSON") from error
            if process_record != inventory.get("remote_process"):
                raise ValueError("remote process file and inventory record disagree")
        elif name == "remote-launch-failure.json":
            try:
                failure = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("remote launch failure metadata is not valid JSON") from error
            if failure.get("status") != "failed_quarantined":
                raise ValueError("remote launch failure record is not quarantined")
        verified[name] = dict(sha256=digest.hexdigest(), bytes=size)
    return verified


def collect(*, attempt, launch_sha256, password=None, known_hosts=None):
    attempt, launch_record = validate_launch(attempt, launch_sha256)
    receipt_path = attempt / "native-download-receipt.json"
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        if receipt.get("launch_sha256") != launch_sha256:
            raise ValueError("existing archive receipt belongs to another launch")
        validate_remote_metadata(attempt, launch_record, receipt.get("remote_inventory", {}))
        if receipt.get("metadata") != receipt["remote_inventory"].get("metadata"):
            raise ValueError("previously collected metadata receipt differs")
        for relative, metadata in receipt.get("downloaded_files", {}).items():
            safe_relative(relative)
            path = attempt / "selection" / Path(*PurePosixPath(relative).parts)
            if path.is_symlink() or not path.is_file() or sha(path) != metadata["sha256"]:
                raise ValueError("previously collected archive differs: " + relative)
        return receipt
    client = ssh_client(launch_record["host"], launch_record["username"], password=password,
        known_hosts=known_hosts)
    try:
        runtime_config = read_json(attempt / "remote-config.json")
        runtime_config["config_sha256"] = launch_record["config_sha256"]
        command = remote_inventory_command(runtime_config, python=launch_record["remote_python"])
        inventory = json.loads(execute_remote(client, command, timeout=180))
        if inventory.get("schema") != "FairFinalActiveSelectionRemote/v1":
            raise ValueError("remote inventory schema differs")
        validate_remote_metadata(attempt, launch_record, inventory)
        state = inventory["state"]
        if state == "running" or state == "orphaned_process_live":
            poll_path = attempt / ("remote-poll-" + str(time.time_ns()) + ".json")
            write_json(poll_path, inventory)
            return dict(status=state, poll_file=str(poll_path),
                training_admissible=False, formal_g2_pass=False, gates_closed=False)
        expected = expected_remote_tree(attempt, launch_record)
        files = inventory.get("files")
        if not isinstance(files, dict):
            raise ValueError("remote artifact inventory is absent")
        verify_remote_mirror(files, expected)
        sftp = client.open_sftp()
        downloaded = {}
        try:
            verified_metadata = verify_remote_metadata_files(sftp, attempt, launch_record, inventory)
            for relative, metadata in files.items():
                path = safe_relative(relative)
                if path.parts[0] in ("inputs", "deployment"):
                    continue
                remote = str(PurePosixPath(launch_record["remote_root"]) / "selection" / path)
                local = attempt / "selection" / Path(*path.parts)
                local.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = local.with_name(local.name + "." + str(time.time_ns()) + ".partial")
                digest = hashlib.sha256()
                size = 0
                with sftp.open(remote, "rb") as source, temporary.open("xb") as target:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        target.write(block)
                        digest.update(block)
                        size += len(block)
                    target.flush()
                    os.fsync(target.fileno())
                actual = dict(sha256=digest.hexdigest(), bytes=size)
                if actual != metadata:
                    temporary.unlink()
                    raise ValueError("downloaded remote artifact differs: " + relative)
                os.replace(temporary, local)
                downloaded[relative] = actual
        finally:
            sftp.close()
        for required in ("execution.json",):
            if state == "terminal_archive_ready_for_independent_verification" and required not in downloaded:
                raise ValueError("terminal remote archive omits " + required)
        execution_path = attempt / "selection/execution.json"
        completion_path = attempt / "selection/completion.json"
        receipt = dict(schema="FairFinalActiveSelectionRemoteReceipt/v1", status=state,
            launch_sha256=launch_sha256, remote_inventory=inventory,
            downloaded_files=downloaded, metadata=verified_metadata,
            execution_sha256=sha(execution_path) if execution_path.is_file() else None,
            completion_sha256=sha(completion_path) if completion_path.is_file() else None,
            training_admissible=False, formal_g2_pass=False, gates_closed=False)
        write_json(receipt_path, receipt)
        return receipt
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("handoff", type=Path)
    launch_parser.add_argument("--handoff-sha", required=True)
    launch_parser.add_argument("--host", required=True)
    launch_parser.add_argument("--username", required=True)
    launch_parser.add_argument("--password-env")
    launch_parser.add_argument("--known-hosts", type=Path)
    launch_parser.add_argument("--private-root", type=Path, required=True)
    launch_parser.add_argument("--remote-base", required=True)
    launch_parser.add_argument("--remote-python", required=True)
    launch_parser.add_argument("--remote-source", required=True)
    launch_parser.add_argument("--remote-data-root", required=True)
    launch_parser.add_argument("--gpu-index", type=int, default=1)
    launch_parser.add_argument("--timeout-seconds", type=int, default=28800)
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("attempt", type=Path)
    collect_parser.add_argument("--launch-sha", required=True)
    collect_parser.add_argument("--password-env")
    collect_parser.add_argument("--known-hosts", type=Path)
    args = parser.parse_args()
    password = os.environ.get(args.password_env) if args.password_env else None
    if args.password_env and password is None:
        parser.error("the requested password environment variable is unset")
    if args.command == "launch":
        result = launch(handoff=args.handoff, handoff_sha256=args.handoff_sha, host=args.host,
            username=args.username, password=password, known_hosts=args.known_hosts,
            private_root=args.private_root, remote_base=args.remote_base,
            remote_python=args.remote_python, remote_source=args.remote_source,
            remote_data_root=args.remote_data_root, gpu_index=args.gpu_index,
            timeout_seconds=args.timeout_seconds)
    else:
        result = collect(attempt=args.attempt, launch_sha256=args.launch_sha,
            password=password, known_hosts=args.known_hosts)
    print(json.dumps(result, sort_keys=True, default=str), flush=True)


if __name__ == "__main__":
    main()
