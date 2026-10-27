"""Run synthetic smoke directly in an ARM64 Linux guest; never load the game."""
import argparse
import hashlib
import json
import platform
import subprocess
import tempfile
from pathlib import Path

from elf_audit import Elf64

EXPECTED = ["process_syscall", "monotonic_clock", "file_io", "unix_socket_ipc",
            "clone_thread_shared_state", "tls_isolation", "signal_delivery", "all_checks"]
CANARY = b"Nashura direct ARM64 Linux canary\n\0"


def validate(stdout, returncode, names=EXPECTED, expected_exit=0):
    try:
        records = [json.loads(line) for line in stdout.splitlines()]
    except (ValueError, UnicodeError):
        return False
    return (returncode == expected_exit and
            records == [{"check": name, "pass": True} for name in names] and
            all(record["pass"] is True for record in records))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"schema": "nashura-direct-arm64-smoke.v1", "synthetic": True,
              "native_game_verified": False, "status": "BLOCKED_ENVIRONMENT",
              "system": platform.system(), "arch": platform.machine(),
              "kernel": platform.release(), "checks": EXPECTED}
    exit_code = 2
    try:
        if platform.system() != "Linux" or platform.machine().lower() not in {"aarch64", "arm64"}:
            raise ValueError("requires direct ARM64 Linux guest execution")
        report["status"] = "BLOCKED_INPUT"
        data = args.elf.read_bytes()
        report["elf_sha256"] = hashlib.sha256(data).hexdigest()
        if report["elf_sha256"] != args.sha256:
            raise ValueError("ELF identity mismatch")
        elf = Elf64(data)
        if elf.machine != 183 or elf.kind not in {2, 3}:
            raise ValueError("requires AArch64 executable ELF")
        report["status"] = "FAIL"
        with tempfile.TemporaryDirectory(prefix="execution-", dir=args.output) as directory:
            results = []
            for name, extra in [("normal", []), ("exit23", ["--exit-23"])]:
                command = [str(args.elf.resolve()), *extra]
                try:
                    result = subprocess.run(command, cwd=directory, capture_output=True, timeout=20)
                    stdout, stderr, code = result.stdout, result.stderr, result.returncode
                except subprocess.TimeoutExpired as error:
                    stdout, stderr, code = error.stdout or b"", error.stderr or b"", None
                for suffix, value in [("stdout", stdout), ("stderr", stderr)]:
                    (args.output / (name + "." + suffix)).write_bytes(value)
                results.append({"command": command, "exit_code": code,
                                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                                "stderr_sha256": hashlib.sha256(stderr).hexdigest()})
                if name == "normal":
                    normal_ok = validate(stdout, code)
                else:
                    exit_ok = validate(stdout, code, ["exit_code_probe"], 23)
            canary = Path(directory) / "smoke-file.txt"
            canary_ok = canary.is_file() and canary.read_bytes() == CANARY
            report.update(runs=results, file_canary_verified=canary_ok)
            passed = normal_ok and exit_ok and canary_ok
            report["status"] = "PURE_LINUX_SMOKE_PASS" if passed else "FAIL"
            exit_code = 0 if passed else 1
    except (ValueError, OSError) as error:
        report["error"] = str(error)
    with (args.output / "report.json").open("x") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
