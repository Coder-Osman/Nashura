"""Run isolated native-candidate lease experiments; no engine or Android parity claim."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import signal
import subprocess
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args):
    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise ValueError("native ARM64 Linux required")
    if digest(args.probe) != args.probe_sha256 or digest(args.linker) != args.linker_sha256:
        raise ValueError("executable identity mismatch")
    args.output.mkdir(exist_ok=False)
    state = args.output / "state-directory"
    state.mkdir()
    inputs = [args.probe, args.linker, *sorted(args.library_path.glob("*.so"))]
    before = {str(path): digest(path) for path in inputs}
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("LD_", "NASHURA_"))}
    environment["LD_LIBRARY_PATH"] = str(args.library_path)
    rows = []
    processes = []

    def command(mode):
        return [str(args.linker), str(args.probe), str(state), mode]

    def record(name, process, expected):
        stdout, stderr = process.communicate(timeout=10)
        (args.output / (name + ".stdout")).write_bytes(stdout)
        (args.output / (name + ".stderr")).write_bytes(stderr)
        row = {"case": name, "mode": process.args[-1], "exit_code": process.returncode,
               "expected_exit_code": expected, "passed": process.returncode == expected,
               "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
               "stderr_sha256": hashlib.sha256(stderr).hexdigest()}
        rows.append(row)
        if not row["passed"]:
            raise RuntimeError("unexpected native probe exit")

    def start(mode):
        process = subprocess.Popen(command(mode), env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        processes.append(process)
        return process

    def holder():
        process = start("hold")
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(10):
                raise RuntimeError("lease acquisition timed out")
            marker = process.stdout.readline()
        (args.output / ("holder-" + str(len(rows)) + ".marker")).write_bytes(marker)
        if marker != b"LEASE_HELD\n":
            raise RuntimeError("native holder failed to acquire lease")
        return process

    failure = None
    state_before = None
    try:
        record("fresh", start("fresh"), 0)
        state_before = digest(state / "state")
        owner = holder()
        contenders = [start("restore") for _ in range(5)]
        for index, process in enumerate(contenders):
            record("contender-" + str(index), process, 3)
        owner.stdin.write(b"x")
        owner.stdin.flush()
        record("normal-release", owner, 0)
        record("restore-after-release", start("restore"), 0)
        owner = holder()
        owner.send_signal(signal.SIGKILL)
        record("killed-owner", owner, -signal.SIGKILL)
        record("restore-after-kill", start("restore"), 0)
        record("unknown-rejected", start("unknown"), 0)
        if digest(state / "state") != state_before:
            raise RuntimeError("state changed during lease-only operations")
    except Exception as error:
        failure = type(error).__name__ + ": " + str(error)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                stdout, stderr = process.communicate(timeout=10)
                name = "cleanup-" + str(process.pid)
                (args.output / (name + ".stdout")).write_bytes(stdout)
                (args.output / (name + ".stderr")).write_bytes(stderr)
    after = {str(path): digest(path) for path in inputs}
    result = {"schema": "nashura.config-lease-experiment.v1", "machine": platform.machine(),
              "kernel": platform.release(), "rows": rows, "failure": failure,
              "input_hashes_before": before, "input_hashes_after": after,
              "inputs_unchanged": before == after, "state_sha256": digest(state / "state")
              if (state / "state").exists() else None, "initial_state_sha256": state_before,
              "scope": "native candidate lease and persistence only",
              "android_parity": "NOT_RUN", "catalog_ready_pass": False}
    with (args.output / "result.json").open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return int(failure is not None or before != after)


def main():
    parser = argparse.ArgumentParser()
    for name in ("probe", "linker", "library-path", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--probe-sha256", required=True)
    parser.add_argument("--linker-sha256", required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
