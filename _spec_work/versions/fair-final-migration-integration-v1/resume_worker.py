import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def alive(process):
    path = Path("/proc") / str(process["pid"]) / "stat"
    if not path.exists():
        return False
    fields = path.read_text().rsplit(")", 1)[1].split()
    return fields[19] == process["start_ticks"] and fields[0] != "Z"


def publish(path, value):
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


def main():
    root = Path(sys.argv[1])
    settings = read(root / "resume-settings.json")
    parent = Path(settings["parent"])
    phases = []
    started = time.monotonic()
    child = None
    try:
        for name, expected in settings["payload_sha256"].items():
            if sha(root / name) != expected:
                raise ValueError("resume probe payload differs")
        while True:
            completion = parent / "completion.json"
            running = alive(settings["parent_process"])
            if completion.exists() and not running:
                break
            if not running and not completion.exists():
                raise ValueError("parent stopped without a completion record")
            if time.monotonic() - started > 2100:
                raise TimeoutError("parent completion wait budget exceeded")
            publish(root / "status.json", dict(phase="waiting_for_parent", parent_alive=running,
                seconds=time.monotonic()-started, training_admissible=False))
            time.sleep(5)
        completion = read(parent / "completion.json")
        if completion["exit_code"] != 0 or completion["training_admissible"] is not False:
            raise ValueError("parent training did not complete successfully")
        report = read(parent / "round0.report.json")
        checkpoint_sha = sha(parent / "round0.pt")
        if (report["checkpoint_sha"] != checkpoint_sha or report["latest_optimizer_steps"] != 1
                or report["terminal"] is not None or report["training_admissible"] is not False):
            raise ValueError("parent single-update checkpoint identity differs")
        for name, expected in read(parent / "deployment.json").items():
            if sha(parent / name) != expected or sha(root / name) != expected:
                raise ValueError("parent and probe frozen deployments differ")
        for filename in ("round0.pt", "round0.report.json", "completion.json"):
            shutil.copyfile(parent / filename, root / ("parent-" + filename))
        if sha(root / "parent-round0.pt") != checkpoint_sha:
            raise ValueError("parent snapshot transfer differs")
        publish(root / "parent-receipt.json", dict(checkpoint_sha256=checkpoint_sha,
            report_sha256=sha(parent / "round0.report.json"), completion_sha256=sha(parent / "completion.json"),
            parent=str(parent), dataset_identity=report["dataset_identity"], training_admissible=False))
        probe = root / "_spec_work/versions/fair-final-migration-integration-v1/probe_native_resume.py"
        outputs = root / "comparison"
        for mode in ("reference", "resumed"):
            checkpoint = root / "parent-round0.pt" if mode == "reference" else outputs / "midpoint.pt"
            pinned = checkpoint_sha if mode == "reference" else read(outputs / "reference.json")["midpoint_sha256"]
            command = [sys.executable, "-B", "-u", str(probe), "--root", str(root),
                "--data-root", settings["data_root"], "--source", settings["source"],
                "--checkpoint", str(checkpoint), "--checkpoint-sha", pinned,
                "--output", str(outputs), "--mode", mode]
            if mode == "resumed":
                command += ["--reference-sha", sha(outputs / "reference.json")]
            with (root / (mode + ".log")).open("x") as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=root)
                process = dict(pid=child.pid,
                    start_ticks=(Path("/proc") / str(child.pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
                publish(root / "status.json", dict(phase=mode, process=process,
                    command=command, training_admissible=False))
                try:
                    code = child.wait(timeout=3600)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    raise TimeoutError("bounded resume phase exceeded 3600 seconds")
            phases.append(dict(phase=mode, exit_code=code, process=process))
            publish(root / "phase-results.json", phases)
            if code:
                raise ValueError("resume phase failed: " + mode)
        result = read(outputs / "resumed.json")
        if result["exact_model_optimizer_rng_sampler_selection"] is not True:
            raise ValueError("resume comparison did not pass")
        publish(root / "completion.json", dict(status="passed_pending_independent_verification",
            phases=phases, seconds=time.monotonic()-started, training_admissible=False, formal_g2_pass=False))
    except Exception as error:
        publish(root / "completion.json", dict(status="failed", error=repr(error), phases=phases,
            seconds=time.monotonic()-started, training_admissible=False, formal_g2_pass=False))
        raise
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
