import argparse
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import signal
import subprocess
import sys
import time


SCHEMA = "FairFinalActiveSelectionRemote/v1"


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


def validate_config(config):
    required = {
        "schema", "attempt_id", "remote_root", "remote_base", "python", "source", "source_sha256",
        "data_root", "gpu_index", "gpu_uuid", "timeout_seconds", "handoff_sha256", "worker_sha256",
        "runtime_sha256", "uploaded_files",
    }
    if set(config) != required or config["schema"] != SCHEMA:
        raise ValueError("remote selection configuration schema differs")
    if (not isinstance(config["attempt_id"], str) or not config["attempt_id"].startswith("selection-")
            or type(config["timeout_seconds"]) is not int or not 1800 <= config["timeout_seconds"] <= 28800
            or type(config["gpu_index"]) is not int or not 0 <= config["gpu_index"] < 8):
        raise ValueError("remote selection identity, budget, or GPU differs")
    for name in ("remote_root", "remote_base", "python", "source", "data_root"):
        value = config[name]
        if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
            raise ValueError("remote selection path is not absolute")
        if ".." in Path(value).parts or str(PurePosixPath(value)) != value:
            raise ValueError("remote selection path traverses parent")
    if (PurePosixPath(config["remote_root"]).parent != PurePosixPath(config["remote_base"])
            or PurePosixPath(config["remote_root"]).name != config["attempt_id"]):
        raise ValueError("remote attempt root is outside its pinned base")
    for name in ("source_sha256", "handoff_sha256", "worker_sha256", "runtime_sha256"):
        value = config[name]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("remote selection digest differs")
    if config["gpu_uuid"] is not None and not isinstance(config["gpu_uuid"], str):
        raise ValueError("remote GPU UUID differs")
    if not isinstance(config["uploaded_files"], dict) or not config["uploaded_files"]:
        raise ValueError("remote upload manifest is empty")
    for name, digest in config["uploaded_files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise ValueError("remote upload path is unsafe")
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("remote upload digest differs")


def command_output(command, *, environment=None, run=subprocess.run):
    result = run(command, check=True, capture_output=True, text=True, env=environment, timeout=90)
    return result.stdout.strip()


def gpu_inventory(*, run=subprocess.run):
    output = command_output(["nvidia-smi", "--query-gpu=index,uuid,name,memory.used",
        "--format=csv,noheader,nounits"], run=run)
    devices = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) < 4 or not row[0].strip().isdigit() or not row[-1].strip().isdigit():
            raise ValueError("nvidia-smi GPU inventory is malformed")
        devices.append(dict(index=int(row[0]), uuid=row[1].strip(), name=",".join(row[2:-1]).strip(),
            memory_used_mib=int(row[-1])))
    if not devices or len({device["index"] for device in devices}) != len(devices):
        raise ValueError("nvidia-smi GPU inventory is empty or duplicated")
    return devices


def gpu_compute_processes(*, run=subprocess.run):
    output = command_output(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid",
        "--format=csv,noheader,nounits"], run=run)
    processes = []
    if output in ("", "No running processes found."):
        return processes
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) != 2 or not row[0].strip().isdigit() or not row[1].strip():
            raise ValueError("nvidia-smi compute process inventory is malformed")
        processes.append(dict(pid=int(row[0]), gpu_uuid=row[1].strip()))
    return processes


def resource_preflight(config, *, run=subprocess.run, environ=None, meminfo_path="/proc/meminfo",
        proc_root="/proc"):
    validate_config(config)
    environ = dict(os.environ if environ is None else environ)
    if not Path(proc_root).is_dir():
        raise RuntimeError("Linux process identity support is required")
    values = {}
    for line in Path(meminfo_path).read_text().splitlines():
        if line.startswith("MemAvailable:"):
            values["available_gib"] = int(line.split()[1]) / 1024**2
            break
    if values.get("available_gib", 0) < 32:
        raise RuntimeError("remote available memory is below 32 GiB")
    base = Path(config["remote_base"])
    data_root = Path(config["data_root"])
    if (not base.is_dir() or not data_root.is_dir() or base.resolve() != base
            or data_root.resolve() != data_root):
        raise RuntimeError("remote base or complete data root is absent or symlinked")
    base_free = shutil.disk_usage(base).free / 1024**3
    data_free = shutil.disk_usage(data_root).free / 1024**3
    if min(base_free, data_free) < 5:
        raise RuntimeError("remote base or data volume has less than 5 GiB free")
    values.update(base_free_gib=base_free, data_free_gib=data_free)
    if sha(config["source"]) != config["source_sha256"]:
        raise RuntimeError("remote source checkpoint SHA differs")
    devices = gpu_inventory(run=run)
    selected = next((device for device in devices if device["index"] == config["gpu_index"]), None)
    if selected is None:
        raise RuntimeError("requested physical GPU is absent")
    if config["gpu_uuid"] is not None and selected["uuid"] != config["gpu_uuid"]:
        raise RuntimeError("physical GPU UUID changed since preflight")
    if selected["memory_used_mib"] > 1024:
        raise RuntimeError("selected physical GPU has more than 1 GiB in use")
    processes = [process for process in gpu_compute_processes(run=run)
        if process["gpu_uuid"] == selected["uuid"]]
    if processes:
        raise RuntimeError("selected physical GPU has active compute processes")
    environment = dict(environ, CUDA_VISIBLE_DEVICES=selected["uuid"],
        PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    smoke = command_output([config["python"], "-B", "-c",
        "import torch; assert torch.cuda.is_available(); assert torch.cuda.device_count() == 1; "
        "print(torch.cuda.get_device_name(0))"], environment=environment, run=run)
    if not smoke:
        raise RuntimeError("CUDA smoke test did not report a device")
    values.update(gpu=selected, compute_processes=processes, cuda_smoke_device=smoke,
        checked_at=time.time(), status="passed_preflight_only")
    return values


def process_identity(pid):
    stat = Path("/proc", str(pid), "stat")
    if not stat.exists():
        return dict(pid=pid, start_ticks=None)
    fields = stat.read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, start_ticks=fields[19])


def process_matches(identity, *, proc_root=Path("/proc")):
    if (not isinstance(identity, dict) or type(identity.get("pid")) is not int or identity["pid"] <= 0
            or not isinstance(identity.get("start_ticks"), str) or not identity["start_ticks"].isdigit()):
        return False
    stat = Path(proc_root) / str(identity["pid"]) / "stat"
    try:
        fields = stat.read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return fields[19] == identity["start_ticks"] and fields[0] != "Z"


def upload_inventory(root):
    root = Path(root)
    config_path = root / "remote-config.json"
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError("uploaded remote configuration is not a regular file")
    config = read_json(config_path)
    validate_config(config)
    expected_names = set(config["uploaded_files"]) | {"remote-config.json"}
    actual = safe_tree_hash(root)
    if set(actual) != expected_names:
        raise ValueError("remote upload tree is incomplete or contains unexpected files")
    inventory = {}
    for name, expected in config["uploaded_files"].items():
        path = root / name
        if path.is_symlink() or not path.is_file() or actual[name]["sha256"] != expected:
            raise ValueError("uploaded remote file differs: " + name)
        inventory[name] = actual[name]
    return inventory


def acquire_gpu_lock(base, uuid):
    if not isinstance(uuid, str) or not uuid:
        raise ValueError("a pinned GPU UUID is required before acquiring the lease")
    token = hashlib.sha256(uuid.encode()).hexdigest()[:24]
    path = Path(base) / (".nashura-selection-gpu-" + token + ".lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise RuntimeError("Nashura already holds the selected GPU lease") from error
    return descriptor, path


def spawn_job(root, config, *, config_sha256):
    root = Path(root)
    configured_root = Path(config["remote_root"])
    if (root.is_symlink() or configured_root.is_symlink() or root != configured_root
            or root.resolve(strict=True) != root):
        raise ValueError("remote attempt root differs")
    config_path = root / "remote-config.json"
    if sha(config_path) != config_sha256 or read_json(config_path) != config:
        raise ValueError("remote selection configuration SHA differs")
    if sha(Path(__file__)) != config["runtime_sha256"]:
        raise ValueError("remote selection runtime differs")
    if sha(root / "run_selection.py") != config["worker_sha256"]:
        raise ValueError("remote selection worker differs")
    if config["gpu_uuid"] is None:
        raise ValueError("remote selection must pin the physical GPU UUID")
    upload_inventory(root)
    sys.path.insert(0, str(root))
    import run_selection

    if run_selection.sha(root / "run_selection.py") != config["worker_sha256"]:
        raise ValueError("remote run_selection import differs")
    run_selection.checked_handoff(root / "inputs", config["handoff_sha256"])
    lease, lease_path = acquire_gpu_lock(config["remote_base"], config["gpu_uuid"])
    child = None
    try:
        resources = resource_preflight(config)
        if (root / "selection").exists() or (root / "remote-process.json").exists():
            raise FileExistsError("remote selection attempt is not new")
        command = [config["python"], "-B", "-u", str(root / "run_selection.py"),
            str(root / "inputs"), "--handoff-file-sha", config["handoff_sha256"],
            "--output", str(root / "selection"), "--source", config["source"],
            "--data-root", config["data_root"], "--timeout-seconds", str(config["timeout_seconds"]),
            "--device", "cuda:0"]
        environment = dict(os.environ, CUDA_VISIBLE_DEVICES=config["gpu_uuid"],
            PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
        with (root / "remote-worker.log").open("xb") as log:
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=root,
                env=environment, start_new_session=True, pass_fds=(lease,))
        identity = process_identity(child.pid)
        write_json(root / "remote-process.json", dict(schema=SCHEMA, process=identity,
            command=command, config_sha256=config_sha256, lease_base=str(config["remote_base"]),
            gpu_uuid=config["gpu_uuid"],
            resources=resources, started_at=time.time(), status="running",
            training_admissible=False, formal_g2_pass=False, gates_closed=False))
        return dict(status="started", process=identity, resources=resources,
            training_admissible=False, formal_g2_pass=False, gates_closed=False)
    except BaseException as error:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
        write_json(root / "remote-launch-failure.json", dict(status="failed_quarantined",
            error=type(error).__name__, training_admissible=False,
            formal_g2_pass=False, gates_closed=False))
        raise
    finally:
        os.close(lease)


def safe_tree_hash(root):
    root = Path(root)
    result = {}
    if not root.exists():
        return result
    if root.is_symlink() or not root.is_dir():
        raise ValueError("remote selection output is not a real directory")
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        if directory_path.is_symlink():
            raise ValueError("remote selection tree contains symlink")
        for name in names:
            if (directory_path / name).is_symlink():
                raise ValueError("remote selection tree contains symlink")
        for name in files:
            path = directory_path / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("remote selection tree contains non-regular file")
            relative = path.relative_to(root).as_posix()
            result[relative] = dict(sha256=sha(path), bytes=path.stat().st_size)
    return result


def inventory(root):
    root = Path(root)
    if root.is_symlink() or root.resolve(strict=True) != root:
        raise ValueError("remote attempt root cannot contain a symlink")
    config_path = root / "remote-config.json"
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError("remote selection configuration is absent or symlinked")
    config = read_json(config_path)
    validate_config(config)
    if config["remote_root"] != str(root):
        raise ValueError("remote selection configuration belongs to another root")
    process_path = root / "remote-process.json"
    if process_path.is_symlink():
        raise ValueError("remote process record cannot be a symlink")
    process_record = read_json(process_path) if process_path.is_file() else None
    if process_record is not None and (process_record.get("schema") != SCHEMA
            or process_record.get("status") != "running"):
        raise ValueError("remote process record schema differs")
    if process_record is not None and (
            process_record.get("lease_base") != config["remote_base"]
            or process_record.get("gpu_uuid") != config["gpu_uuid"]
            or config["gpu_uuid"] is None):
        raise ValueError("remote process lease differs from pinned configuration")
    running = []
    if process_record is not None and process_matches(process_record.get("process")):
        running.append("worker")
    output = root / "selection"
    for name in ("status.json", "completion.json"):
        path = output / name
        if path.is_symlink():
            raise ValueError("remote selection state record cannot be a symlink")
        if path.is_file() and process_matches(read_json(path).get("process")):
            running.append("training_child" if name == "status.json" else "completion_process")
    lease_available = None
    gpu_processes = None
    gpu_state = None
    if process_record is not None and not running:
        try:
            lease, _ = acquire_gpu_lock(process_record["lease_base"], process_record["gpu_uuid"])
        except RuntimeError:
            lease_available = False
            running.append("gpu_lease")
        else:
            os.close(lease)
            lease_available = True
            devices = gpu_inventory()
            selected = next((device for device in devices if device["uuid"] == process_record["gpu_uuid"]), None)
            gpu_processes = [process for process in gpu_compute_processes()
                if process["gpu_uuid"] == process_record["gpu_uuid"]]
            gpu_state = dict(device=selected, compute_processes=gpu_processes)
    if any(name in running for name in ("worker", "training_child", "completion_process", "gpu_lease")):
        state = "running" if "worker" in running else "orphaned_process_live"
        files = {}
    elif process_record is None:
        state = "launch_incomplete"
        files = safe_tree_hash(output)
    else:
        files = safe_tree_hash(output)
    if process_record is not None and not running:
        if "completion.json" in files:
            completion = read_json(output / "completion.json")
            if completion.get("status") != "completed_pending_independent_content_verification":
                state = "quarantined_completion_state"
            else:
                state = "terminal_archive_ready_for_independent_verification"
        elif "failure.json" in files:
            state = "failed_quarantined"
        else:
            state = "stopped_without_terminal"
    metadata = {}
    for name in ("remote-config.json", "remote-process.json", "remote-launch-failure.json"):
        path = root / name
        if path.is_symlink():
            raise ValueError("remote metadata cannot be a symlink: " + name)
        if path.is_file():
            metadata[name] = dict(sha256=sha(path), bytes=path.stat().st_size)
    return dict(schema=SCHEMA, state=state, running=running,
        remote_process=process_record, files=files, timestamp=time.time(),
        metadata=metadata, lease_available=lease_available, gpu_state=gpu_state,
        training_admissible=False, formal_g2_pass=False, gates_closed=False)


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--config", type=Path, required=True)
    spawn_parser = subparsers.add_parser("spawn")
    spawn_parser.add_argument("--root", type=Path, required=True)
    spawn_parser.add_argument("--config-sha", required=True)
    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "preflight":
        result = resource_preflight(read_json(args.config))
    elif args.command == "spawn":
        config_path = args.root / "remote-config.json"
        result = spawn_job(args.root, read_json(config_path), config_sha256=args.config_sha)
    else:
        result = inventory(args.root)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
