import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time

from run_selection import GATES, pinned, publish, read, run_child


def command(root, request):
    return [sys.executable, "-B", "-u", str(Path(root) / "probe_active_resume.py"),
        "--root", str(root), "--data-root", request["data_root"],
        "--source", request["source"], "--mode", "trainable",
        "--reference-sha", request["files"]["active-comparison/reference.json"]["sha256"]]


def execute(root, expected_sha):
    root = Path(root).resolve()
    pinned(root / "retry-input.json", expected_sha)
    request = read(root / "retry-input.json")
    if (request["schema"] != "FairFinalActiveResumeRetry/v1"
            or any(request[name] is not value for name, value in GATES.items())
            or type(request["timeout_seconds"]) is not int
            or not 1800 <= request["timeout_seconds"] <= 86400):
        raise ValueError("bounded engineering retry required")
    started = time.monotonic()
    with Path("/tmp/nashura-active-retry-gpu-" + str(request["device_index"]) + ".lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            for name, metadata in request["files"].items():
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
                    raise ValueError("retry input path differs")
                pinned(root / name, metadata["sha256"])
                if (root / name).stat().st_size != metadata["bytes"]:
                    raise ValueError("retry input size differs")
            for name, expected in read(root / "deployment.json").items():
                pinned(root / name, expected)
            used = subprocess.check_output(["nvidia-smi", "-i", str(request["device_index"]),
                "--query-gpu=memory.used", "--format=csv,noheader,nounits"], text=True)
            if int(used.strip()) > 1024:
                raise ValueError("retry GPU is occupied")
            invocation = command(root, request)
            environment = dict(CUDA_VISIBLE_DEVICES=str(request["device_index"]),
                PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", CUBLAS_WORKSPACE_CONFIG=":4096:8")
            code, process = run_child(invocation, root=root, environment=environment,
                timeout_seconds=request["timeout_seconds"] - (time.monotonic() - started))
            phases = [dict(phase="trainable", exit_code=code, process=process)]
            publish(root / "phase-results.json", phases)
            if code:
                raise RuntimeError("independent trainable recovery failed: " + str(code))
            report = read(root / "active-comparison/trainable.json")
            pinned(root / "active-comparison/trainable-resumed.pt", report["resumed_checkpoint_sha256"])
            if report["process"] != process or report["status"] != "passed_limited_scope":
                raise ValueError("retry report process or status differs")
            for name, metadata in request["files"].items():
                pinned(root / name, metadata["sha256"])
            publish(root / "completion.json", dict(exit_code=0,
                status="completed_retry_pending_independent_verification", process=process,
                retry_input_sha256=expected_sha, command=invocation, environment=environment,
                seconds=time.monotonic() - started, **GATES))
        except BaseException as error:
            publish(root / "supervisor-failure.json", dict(error=repr(error),
                seconds=time.monotonic() - started, **GATES))
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--input-sha", required=True)
    args = parser.parse_args()
    execute(args.root, args.input_sha)
