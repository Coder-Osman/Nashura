import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
INTEGRATION = "_spec_work/versions/fair-final-migration-integration-v1"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True))
    temporary.replace(path)


def process_identity(process):
    fields = Path("/proc", str(process.pid), "stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=process.pid, start_ticks=fields[19])


def matching_process(pid, start_ticks):
    try:
        fields = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] == start_ticks and fields[0] != "Z"
    except FileNotFoundError:
        return False


def stop(process, *, grace=25):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    return process.returncode


def check_port(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def run_phase(root, phase, command, *, timeout, environment):
    started = time.monotonic()
    identity = None
    with (root / (phase + ".log")).open("x") as output:
        child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
            cwd=root, env=environment)
        try:
            identity = process_identity(child)
            write(root / "status.json", dict(phase=phase, process=identity, command=command,
                training_admissible=False))
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise TimeoutError(phase + " exceeded bounded deadline") from error
        finally:
            stop(child)
            write(root / (phase + "-process.json"), dict(
                process=identity or dict(pid=child.pid, start_ticks=None),
                exit_code=child.returncode, seconds=time.monotonic() - started))
    if code:
        raise RuntimeError(phase + " failed: " + str(code))


def clean_engine(engine, instance, port, *, expected_identity):
    stop(engine)
    path = instance / "process.json"
    if not path.exists():
        check_port(port)
        return dict(launcher=expected_identity, exit_code=engine.returncode,
            process_record_absent=True, port_released=True, clean=False)
    state = json.loads(path.read_text())
    if expected_identity is None or (state.get("pid") != expected_identity["pid"]
            or state.get("start_ticks") != expected_identity["start_ticks"]):
        raise ValueError("owned engine launcher identity differs")
    child_pid, child_ticks = state.get("child_pid"), state.get("child_start_ticks")
    if child_pid is not None:
        if type(child_pid) is not int or child_pid <= 0 or not isinstance(child_ticks, str):
            raise ValueError("owned engine child identity absent")
        if matching_process(child_pid, child_ticks):
            os.kill(child_pid, signal.SIGTERM)
            until = time.monotonic() + 10
            while matching_process(child_pid, child_ticks) and time.monotonic() < until:
                time.sleep(.1)
            if matching_process(child_pid, child_ticks):
                os.kill(child_pid, signal.SIGKILL)
                until = time.monotonic() + 10
                while matching_process(child_pid, child_ticks) and time.monotonic() < until:
                    time.sleep(.1)
            if matching_process(child_pid, child_ticks):
                raise RuntimeError("owned native engine remained alive after cleanup")
    check_port(port)
    return dict(launcher=expected_identity, exit_code=engine.returncode,
        process=state, port_released=True, clean=state.get("phase") == "stopped")


def validate_request(request, *, root, deployment_root):
    from collect_selected import argument_parser

    if set(request) != {"schema", "collector_args", "engine"} or request["schema"] != "FairFinalSelectedCollectionRun/v1":
        raise ValueError("selected collection request schema differs")
    arguments = request["collector_args"]
    if not isinstance(arguments, list) or not arguments or any(not isinstance(item, str) for item in arguments):
        raise ValueError("explicit collector arguments required")
    options = argument_parser().parse_args(arguments)
    if options.preflight_only or options.output.resolve() != root / "data":
        raise ValueError("worker owns collection output and preflight lifecycle")
    if not 1 <= options.port <= 65535:
        raise ValueError("native port outside valid range")
    engine = request["engine"]
    if set(engine) != {"project", "probe", "launcher", "launcher_sha256", "service_sha256"}:
        raise ValueError("explicit pinned native engine inputs required")
    expected_launcher = deployment_root / "phase_snapshot_launcher.py"
    if Path(engine["launcher"]).resolve() != expected_launcher:
        raise ValueError("collection requires deployed identity-recording engine launcher")
    expected = {expected_launcher: engine["launcher_sha256"],
        Path(engine["project"]) / "research/standalone-resident-p18-stepatomic/service.py": engine["service_sha256"],
        Path(engine["probe"]): options.engine_sha}
    for path, digest in expected.items():
        if sha(path) != digest:
            raise ValueError("pinned collection engine dependency differs: " + str(path))
    return options


def run(root, *, request_path, request_sha, deployment_root, deployment_sha,
        preflight_timeout=3600, collection_timeout=4800, startup_timeout=180):
    root, deployment_root = Path(root).resolve(), Path(deployment_root).resolve()
    if sha(request_path) != request_sha or sha(deployment_root / "deployment.json") != deployment_sha:
        raise ValueError("collection request or deployment identity differs")
    deployed = json.loads((deployment_root / "deployment.json").read_text())
    if (ROOT.resolve() != deployment_root / INTEGRATION
            or not {INTEGRATION + "/" + name for name in
                ("collect_selected.py", "selected_collection_worker.py")}.issubset(deployed)
            or "phase_snapshot_launcher.py" not in deployed):
        raise ValueError("worker and collector must execute from the pinned deployment")
    for name, digest in deployed.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise ValueError("noncanonical collection deployment path")
        if sha(deployment_root / relative) != digest:
            raise ValueError("frozen collection source differs: " + name)
    if any(type(value) is not int or not 1 <= value <= 14400
            for value in (preflight_timeout, collection_timeout, startup_timeout)):
        raise ValueError("finite bounded phase timeouts required")
    request = json.loads(Path(request_path).read_text())
    options = validate_request(request, root=root, deployment_root=deployment_root)
    root.mkdir(parents=True, exist_ok=False)
    write(root / "request.json", request)
    engine = request["engine"]
    collector = [sys.executable, "-B", "-u", str(ROOT / "collect_selected.py"), *request["collector_args"]]
    environment = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    instance = root / "engine"
    started = time.monotonic()
    process, identity, cleanup = None, None, None
    try:
        run_phase(root, "preflight", collector + ["--preflight-only"],
            timeout=preflight_timeout, environment=environment)
        check_port(options.port)
        command = [sys.executable, "-B", "-u", engine["launcher"], "--project", engine["project"],
            "--port", str(options.port), "--instance", str(instance), "--probe", engine["probe"],
            "--engine-sha256", options.engine_sha]
        with (root / "engine.log").open("x") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                cwd=root, env=environment)
            identity = process_identity(process)
            write(root / "engine-process.json", identity)
            from native_runner.training.fair_search.evaluate_policy import wait_for_engine
            wait_for_engine(process, instance, timeout=startup_timeout)
            state = json.loads((instance / "process.json").read_text())
            attestation = state.get("phase_snapshot_engine", {})
            if (state.get("pid") != identity["pid"] or state.get("start_ticks") != identity["start_ticks"]
                    or attestation != dict(path=str(Path(engine["probe"]).resolve()), sha256=options.engine_sha,
                        launcher_sha256=engine["launcher_sha256"], service_sha256=engine["service_sha256"])):
                raise ValueError("running engine attestation differs from request")
            write(root / "engine-ready.json", state)
            run_phase(root, "collection", collector, timeout=collection_timeout, environment=environment)
    finally:
        failure = sys.exception()
        if process is not None:
            try:
                cleanup = clean_engine(process, instance, options.port, expected_identity=identity)
                write(root / "engine-cleanup.json", cleanup)
            except BaseException as error:
                write(root / "cleanup-failure.json", dict(error=repr(error), training_admissible=False))
                write(root / "failure.json", dict(error=repr(error),
                    collection_error=repr(failure) if failure else None,
                    seconds=time.monotonic() - started, training_admissible=False, formal_g2_pass=False))
                raise
        if failure is not None:
            write(root / "failure.json", dict(error=repr(failure), seconds=time.monotonic() - started,
                training_admissible=False, formal_g2_pass=False))
    if cleanup is None or not cleanup["clean"]:
        write(root / "failure.json", dict(error="native collection requires clean owned engine shutdown",
            seconds=time.monotonic() - started, training_admissible=False, formal_g2_pass=False))
        raise ValueError("native collection requires clean owned engine shutdown")
    report_path = root / "data/attempts" / options.match_id / str(options.attempt_id) / "report.json"
    if not report_path.is_file():
        write(root / "failure.json", dict(error="native collector report absent",
            seconds=time.monotonic() - started, training_admissible=False, formal_g2_pass=False))
        raise ValueError("native collector report absent")
    write(root / "completion.json", dict(schema="FairFinalSelectedCollectionRunResult/v1",
        status="collected_requires_independent_verification", seconds=time.monotonic() - started,
        request_sha256=request_sha, deployment_sha256=deployment_sha,
        report_sha256=sha(report_path), match_id=options.match_id,
        training_admissible=False, formal_g2_pass=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha", required=True)
    parser.add_argument("--deployment-root", type=Path, required=True)
    parser.add_argument("--deployment-sha", required=True)
    parser.add_argument("--preflight-timeout", type=int, default=3600)
    parser.add_argument("--collection-timeout", type=int, default=4800)
    parser.add_argument("--startup-timeout", type=int, default=180)
    args = parser.parse_args()

    def interrupt(*unused):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    run(args.root, request_path=args.request, request_sha=args.request_sha,
        deployment_root=args.deployment_root, deployment_sha=args.deployment_sha,
        preflight_timeout=args.preflight_timeout, collection_timeout=args.collection_timeout,
        startup_timeout=args.startup_timeout)


if __name__ == "__main__":
    main()
