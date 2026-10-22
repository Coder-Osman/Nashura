import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import time


SCHEMA = "FairFinalActiveSelectionExecution/v1"
SERIES = "_spec_work/versions/fair-final-migration-series-v1"
SELECTION = "_spec_work/versions/fair-final-migration-selection-v1"
INTEGRATION = "_spec_work/versions/fair-final-migration-integration-v1"
INPUTS = ("payload.tar.gz", "parent-checkpoint.pt", "parent-verification.json")
GATES = dict(training_admissible=False, formal_g2_pass=False, gates_closed=False)


def sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def publish(path, value, *, replace=False):
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("xb") as handle:
        handle.write(json.dumps(value, sort_keys=True, allow_nan=False).encode())
        handle.flush()
        os.fsync(handle.fileno())
    if replace:
        os.replace(temporary, path)
    else:
        os.link(temporary, path)
        temporary.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def pinned(path, expected):
    if Path(path).is_symlink() or sha(path) != expected:
        raise ValueError("pinned selection file differs: " + Path(path).name)


def checked_handoff(directory, expected_sha):
    directory = Path(directory)
    pinned(directory / "selection-handoff.json", expected_sha)
    request = read(directory / "selection-handoff.json")
    if (request["schema"] != "FairFinalActiveSelectionHandoff/v1"
            or request["status"] != "prepared_only_not_executed"
            or request["resume_optimizer_rng_cursor_history"] is not True
            or request["max_updates"] is not None
            or any(request[name] is not value for name, value in GATES.items())
            or request["parent_checkpoint_state"]["steps"] != 7
            or request["split_rows"] != dict(train=352, validation=358, test=334)
            or set(request["files"]) != set(INPUTS)):
        raise ValueError("verified native selection handoff required")
    for name, metadata in request["files"].items():
        path = directory / name
        pinned(path, metadata["sha256"])
        if path.stat().st_size != metadata["bytes"]:
            raise ValueError("selection handoff byte count differs")
    proof = read(directory / "parent-verification.json")
    if (proof["status"] != "passed_active_archive_and_two_stage_checkpoint_scope"
            or proof["content"]["native_schedule_verified"] is not True
            or proof["frozen_deployment_verified"] is not True
            or proof["content"]["branches"]["trainable"]["state"] != request["parent_checkpoint_state"]
            or any(proof[name] is not value for name, value in GATES.items())
            or any(proof[name] != request[name] for name in ("dataset_identity", "split_rows", "series_sha"))
            or proof["launch_sha256"] != request["parent_launch_sha256"]
            or proof["receipt_sha256"] != request["parent_receipt_sha256"]
            or request["files"]["parent-checkpoint.pt"]["sha256"] != request["parent_checkpoint_sha256"]):
        raise ValueError("selection handoff differs from verified parent")
    return request


def payload_files(directory, request):
    package = Path(directory) / "payload.tar.gz"
    pinned(package, request["files"]["payload.tar.gz"]["sha256"])
    files = {}
    with tarfile.open(fileobj=io.BytesIO(package.read_bytes()), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            if (not member.isfile() or relative.is_absolute() or ".." in relative.parts
                    or str(relative) != member.name or member.name in files):
                raise ValueError("selection payload member differs")
            files[member.name] = archive.extractfile(member).read()
    manifest = json.loads(files["deployment.json"])
    actual = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        if name != "deployment.json"}
    if actual != manifest:
        raise ValueError("selection payload deployment differs")
    required = (SELECTION + "/train_round.py", SERIES + "/deterministic_runtime.py", "private-plan.json")
    if any(name not in files for name in required):
        raise ValueError("selection payload omits frozen entrypoint or runtime")
    for name, expected in (("selection-config.json", request["config"]),
            ("loss-weights.json", request["loss_weights"])):
        if json.loads(files[name]) != expected:
            raise ValueError("selection payload configuration differs")
    plan = json.loads(files["private-plan.json"])
    if (len(plan["rounds"]) != 1
            or plan["rounds"][0]["manifest"]["identity"]["source_a_sha"] != request["dataset_identity"]["source_a_sha"]):
        raise ValueError("selection frozen source identity differs")
    return files, manifest


def invocation(root, *, source, data_root, device, python=None):
    deployment = Path(root) / "deployment"
    command = [python or sys.executable, "-B", "-u", str(deployment / SELECTION / "train_round.py"),
        "--engineering", "--source", str(source), "--private-plan", str(deployment / "private-plan.json"),
        "--data-roots", str(data_root), "--selection-config", str(deployment / "selection-config.json"),
        "--loss-weights", str(deployment / "loss-weights.json"), "--output", str(Path(root) / "selected.pt"),
        "--seed", "20261022", "--batch-size", "256", "--micro-batch-size", "1", "--device", device]
    environment = dict(PYTHONPATH=os.pathsep.join(str(deployment / name)
        for name in ("firstlight_CR", SERIES, SELECTION, INTEGRATION)),
        PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", CUBLAS_WORKSPACE_CONFIG=":4096:8")
    return command, environment


def process_identity(child):
    stat = Path("/proc", str(child.pid), "stat")
    return dict(pid=child.pid, start_ticks=(stat.read_text().rsplit(")", 1)[1].split()[19]
        if stat.exists() else None))


def stop_child(child, grace_seconds):
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            child.wait()
            return
        time.sleep(min(.02, max(0., deadline - time.monotonic())))
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def run_child(command, *, root, environment, timeout_seconds, grace_seconds=30):
    if timeout_seconds <= 0:
        raise TimeoutError("independent selection budget exhausted before child launch")
    with (Path(root) / "selection.log").open("x") as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=root,
            env=dict(os.environ, **environment), start_new_session=True)
        try:
            process = process_identity(child)
            publish(Path(root) / "status.json", dict(phase="selection", process=process,
                command=command, **GATES))
            try:
                code = child.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                stop_child(child, grace_seconds)
                code = 124
            else:
                stop_child(child, grace_seconds)
            return code, process
        except BaseException:
            stop_child(child, grace_seconds)
            raise


def check_report(root, request):
    root = Path(root)
    report = read(root / "selected.report.json")
    pinned(root / "selected.pt", report["checkpoint_sha"])
    if (report["schema"] != "FairFinalMigrationSelectionReport/v1"
            or report["terminal"] not in ("EARLY_STOP", "MAX_EPOCHS")
            or report["implementation_sha"] != request["implementation_sha"]
            or report["dataset_identity"] != request["dataset_identity"]
            or report["series_sha"] != request["series_sha"] or report["config"] != request["config"]
            or report["weights"] != request["loss_weights"] or report["latest_optimizer_steps"] <= 7
            or report["training_admissible"] is not False or report["formal_g2_pass"] is not False):
        raise ValueError("selection has no matching terminal report")
    return {name: dict(sha256=sha(root / name), bytes=(root / name).stat().st_size)
        for name in ("selected.pt", "selected.report.json")}


def execute(handoff, *, expected_sha, output, source, data_root, timeout_seconds=28800, device="cuda:0"):
    started = time.monotonic()
    handoff, output, source, data_root = [Path(path).resolve()
        for path in (handoff, output, source, data_root)]
    if (type(timeout_seconds) is not int or not 1800 <= timeout_seconds <= 28800
            or device not in ("cpu", "cuda:0")):
        raise ValueError("bounded selection budget or device differs")
    if output.exists() or any(output.is_relative_to(path) for path in (handoff, data_root)):
        raise ValueError("new independent selection output required")
    request = checked_handoff(handoff, expected_sha)
    files, manifest = payload_files(handoff, request)
    pinned(source, request["dataset_identity"]["source_a_sha"])
    if not data_root.is_dir():
        raise ValueError("complete selection data root required")
    output.mkdir(parents=True, exist_ok=False)
    try:
        inputs = output / "inputs"
        inputs.mkdir()
        for name in (*INPUTS, "selection-handoff.json"):
            shutil.copyfile(handoff / name, inputs / name)
        checked_handoff(inputs, expected_sha)
        deployment = output / "deployment"
        deployment.mkdir()
        for name, data in files.items():
            path = deployment / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        shutil.copyfile(inputs / "parent-checkpoint.pt", output / "selected.pt")
        command, environment = invocation(output, source=source, data_root=data_root, device=device)
        command += ["--resume-sha", request["parent_checkpoint_sha256"]]
        publish(output / "execution.json", dict(schema=SCHEMA, handoff_file_sha256=expected_sha,
            handoff_sha=request["handoff_sha"], parent_checkpoint_sha256=request["parent_checkpoint_sha256"],
            root=str(output), source=str(source), data_root=str(data_root), device=device, python=sys.executable,
            command=command, environment=environment, timeout_seconds=timeout_seconds,
            source_sha256=manifest, worker_sha256=sha(__file__), **GATES))
        code, process = run_child(command, root=output, environment=environment,
            timeout_seconds=timeout_seconds - (time.monotonic() - started))
        if code:
            raise RuntimeError("selection child exit code: " + str(code))
        checked_handoff(inputs, expected_sha)
        for name, expected in manifest.items():
            pinned(deployment / name, expected)
        artifacts = check_report(output, request)
        completion = dict(status="completed_pending_independent_content_verification", exit_code=0,
            process=process, artifacts=artifacts, execution_sha256=sha(output / "execution.json"),
            seconds=time.monotonic() - started, **GATES)
        publish(output / "completion.json", completion)
        return completion
    except BaseException as error:
        publish(output / "failure.json", dict(status="failed_quarantined", error=repr(error),
            seconds=time.monotonic() - started, **GATES))
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("handoff", type=Path)
    parser.add_argument("--handoff-file-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=28800)
    parser.add_argument("--device", choices=("cpu", "cuda:0"), default="cuda:0")
    args = parser.parse_args()

    def interrupted(signum, frame):
        raise InterruptedError("selection worker interrupted: " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    result = execute(args.handoff, expected_sha=args.handoff_file_sha, output=args.output,
        source=args.source, data_root=args.data_root, timeout_seconds=args.timeout_seconds, device=args.device)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
