import argparse
import os
from pathlib import Path
import shutil
import signal
import sys
import time

from run_selection import GATES, check_report, invocation, pinned, publish, read, run_child, sha
from selection_remote_runtime import acquire_gpu_lock, process_matches, resource_preflight, safe_tree_hash


def validate(request):
    if (request["schema"] != "FairFinalSelectionTimeoutRetry/v1"
            or type(request["timeout_seconds"]) is not int
            or not 1800 <= request["timeout_seconds"] <= 86400
            or any(request[name] is not value for name, value in GATES.items())
            or request["verification"]["status"] != "passed_timeout_archive_and_checkpoint_content_only"
            or request["verification"]["live_resume_verified"] is not False
            or any(request["verification"][name] is not value for name, value in GATES.items())
            or request["verification"]["checkpoint_sha256"] != request["files"]["selected.pt"]["sha256"]):
        raise ValueError("verified bounded selection-timeout retry required")
    for name in request["files"]:
        path = Path(name)
        if not name or name == "." or path.is_absolute() or ".." in path.parts or str(path) != name:
            raise ValueError("retry input path differs")
    required = {"selected.pt", "failure.json", "execution.json", "status.json"}
    if not required <= set(request["files"]):
        raise ValueError("retry omits timeout checkpoint or process evidence")
    for name in ("parent_remote", "remote"):
        path = Path(request[name])
        if not path.is_absolute() or ".." in path.parts or str(path) != request[name]:
            raise ValueError("retry remote path differs")
    if len(request["parent_processes"]) != 2 or any(
            type(item.get("pid")) is not int or item["pid"] <= 0
            or not isinstance(item.get("start_ticks"), str) or not item["start_ticks"].isdigit()
            for item in request["parent_processes"]):
        raise ValueError("retry parent process identity differs")


def checked_files(root, files):
    root = Path(root)
    if root.resolve(strict=True) != root:
        raise ValueError("retry input root contains symlink")
    for name, metadata in files.items():
        path = root / name
        if path.resolve(strict=True) != path:
            raise ValueError("retry input contains symlink")
        pinned(path, metadata["sha256"])
        if path.stat().st_size != metadata["bytes"]:
            raise ValueError("retry input byte count differs")


def prepare_output(parent, output, request):
    parent, output = Path(parent), Path(output)
    validate(request)
    if (output.resolve() != output or output.exists() or output.is_relative_to(parent)
            or parent.is_relative_to(output)):
        raise ValueError("independent new retry output required")
    checked_files(parent, request["files"])
    output.mkdir(parents=True, exist_ok=False)
    for name in request["files"]:
        if Path(name).parts[0] in ("deployment", "inputs"):
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(parent / name, destination)
    shutil.copyfile(parent / "selected.pt", output / "resume-input.pt")
    shutil.copyfile(parent / "selected.pt", output / "selected.pt")
    publish(output / "retry-input.json", request)


def execute(root, *, request_sha):
    root = Path(root).resolve()
    pinned(root / "request.json", request_sha)
    request = read(root / "request.json")
    validate(request)
    for name, expected in request["runtime_sha256"].items():
        pinned(root / name, expected)
    pinned(root / "retry_selection.py", sha(__file__))
    parent = Path(request["parent_remote"])
    config = read(root / "parent-remote-config.json")
    started = time.monotonic()
    output = root / "selection"
    lease = None
    try:
        if str(root) != request["remote"] or str(parent) != config["remote_root"] + "/selection":
            raise ValueError("retry execution root differs")
        for identity in request["parent_processes"]:
            if process_matches(identity):
                raise ValueError("timeout parent process is still live")
        lease, _ = acquire_gpu_lock(config["remote_base"], config["gpu_uuid"])
        resources = resource_preflight(config)
        prepare_output(parent, output, request)
        original = read(output / "inputs/selection-handoff.json")
        command, environment = invocation(output, source=config["source"],
            data_root=config["data_root"], device="cuda:0")
        command += ["--resume-sha", request["verification"]["checkpoint_sha256"]]
        environment["CUDA_VISIBLE_DEVICES"] = config["gpu_uuid"]
        publish(output / "execution.json", dict(schema="FairFinalSelectionTimeoutExecution/v1",
            request_sha256=request_sha, command=command, environment=environment,
            timeout_seconds=request["timeout_seconds"], resources=resources,
            runtime_sha256=request["runtime_sha256"], **GATES))
        code, process = run_child(command, root=output, environment=environment,
            timeout_seconds=request["timeout_seconds"] - (time.monotonic() - started))
        if code:
            raise RuntimeError("selection retry child exit code: " + str(code))
        checked_files(parent, request["files"])
        checked_files(output, {name: metadata for name, metadata in request["files"].items()
            if Path(name).parts[0] in ("deployment", "inputs")})
        pinned(output / "resume-input.pt", request["verification"]["checkpoint_sha256"])
        artifacts = check_report(output, original)
        publish(output / "completion.json", dict(status="completed_pending_independent_content_verification", exit_code=0,
            process=process, artifacts=artifacts, execution_sha256=sha(output / "execution.json"),
            request_sha256=request_sha, seconds=time.monotonic() - started, **GATES))
    except BaseException as error:
        publish(root / "failure.json", dict(status="failed_quarantined", error=repr(error),
            seconds=time.monotonic() - started, **GATES))
        raise
    finally:
        if lease is not None:
            os.close(lease)


def inventory(root, process, request_sha):
    root = Path(root)
    pinned(root / "request.json", request_sha)
    request = read(root / "request.json")
    validate(request)
    if root.resolve(strict=True) != root or str(root) != request["remote"]:
        raise ValueError("retry inventory root differs")
    for name, expected in request["runtime_sha256"].items():
        pinned(root / name, expected)
    running = ["worker"] if process_matches(process) else []
    status = root / "selection/status.json"
    if status.is_file() and process_matches(read(status).get("process")):
        running.append("training_child")
    lease_available = False
    if not running:
        config = read(root / "parent-remote-config.json")
        try:
            lease, _ = acquire_gpu_lock(config["remote_base"], config["gpu_uuid"])
        except RuntimeError:
            running.append("gpu_lease")
        else:
            os.close(lease)
            lease_available = True
    state = "running" if running else "stopped_without_terminal"
    files = {} if running else safe_tree_hash(root)
    if not running:
        if "failure.json" in files:
            state = "failed_quarantined"
        elif "selection/completion.json" in files:
            state = "terminal_archive_ready_for_independent_verification"
    return dict(state=state, running=running, files=files, process=process,
        request_sha256=request_sha, lease_available=lease_available, timestamp=time.time(), **GATES)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--request-sha", required=True)
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise InterruptedError("selection retry interrupted: " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    execute(args.root, request_sha=args.request_sha)
