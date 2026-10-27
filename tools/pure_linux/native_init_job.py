"""Run an identity-checked, bounded native loader observation in a new directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import time

from elf_audit import Elf64


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def check_inputs(payload):
    identity = json.loads((payload / "input-identity.json").read_text())
    observed = {}
    for name, row in identity["libraries"].items():
        if Path(name).name != name:
            raise ValueError("non-basename input")
        path = payload / name if name in {"linker64", "bionic-probe-v2"} else payload / "libs" / name
        actual = digest(path)
        if actual != row["sha256"]:
            raise ValueError("input identity mismatch: " + name)
        observed[name] = actual
    return observed


def run(command, output, environment, timeout, log_limit=32 * 1024 * 1024):
    started = time.monotonic()
    def limits():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (log_limit, log_limit))
    with (output / "stdout.log").open("xb") as stdout, (output / "stderr.log").open("xb") as stderr:
        process = subprocess.Popen(command, cwd=output, env=environment, stdout=stdout,
                                   stderr=stderr, start_new_session=True, preexec_fn=limits)
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return {"command": command, "exit_code": process.returncode, "timed_out": timed_out,
            "elapsed_seconds": time.monotonic() - started}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--loader-debug", action="store_true")
    parser.add_argument("--strace", action="store_true")
    parser.add_argument("--content-root", action="store_true")
    parser.add_argument("--filesystem", action="store_true")
    arguments = parser.parse_args()
    if arguments.filesystem and not arguments.content_root:
        parser.error("--filesystem requires the content-root predecessor")
    payload, probe, output = arguments.payload.resolve(), arguments.probe.resolve(), arguments.output.resolve()
    before = check_inputs(payload)
    probe_sha = digest(probe)
    output.mkdir(parents=True, exist_ok=False)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    environment = {name: value for name, value in os.environ.items() if not name.startswith("LD_")}
    environment["LD_LIBRARY_PATH"] = str(payload / "libs")
    if arguments.loader_debug:
        environment["LD_DEBUG"] = "2"
        environment["LD_DEBUG_OUTPUT"] = str(output / "linker-debug")
    command = [str(payload / "linker64"), str(probe), str(payload / "libs" / "libg.so")]
    if arguments.content_root:
        if before["libg.so"] != "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783":
            raise ValueError("bootstrap requires the exact investigated engine")
        elf = Elf64((payload / "libs" / "libg.so").read_bytes())
        position = elf.address(0x8001a4, 16)
        prologue = elf.data[position:position + 16]
        if hashlib.sha256(prologue).hexdigest() != "041505b15bf9b5814480d13d3f0d3d424b0ae1ff43e1242fc3ca890e3c436508":
            raise ValueError("bootstrap prologue identity mismatch")
        command.append(prologue.hex())
    if arguments.filesystem:
        position = elf.address(0x12dfed4, 16)
        prologue = elf.data[position:position + 16]
        if hashlib.sha256(prologue).hexdigest() != "82336434962db5e12ee984baca6f26be0c3a8886a0bb147ebaac906144ca24a8":
            raise ValueError("filesystem bootstrap prologue identity mismatch")
        command.append(prologue.hex())
    if arguments.strace:
        command = ["strace", "-ff", "-tt", "-s", "256", "-o", str(output / "syscalls"), *command]
    result = run(command, output, environment, 45)
    after = check_inputs(payload)
    result.update({"inputs_before": before, "inputs_after": after,
                   "inputs_unchanged": before == after, "probe_sha256": probe_sha,
                   "probe_unchanged": digest(probe) == probe_sha,
                   "outputs": {path.name: digest(path) for path in sorted(output.iterdir()) if path.is_file()},
                   "explicit_jni_onload": False, "content_bootstrap_requested": arguments.content_root,
                   "filesystem_bootstrap_requested": arguments.filesystem,
                   "explicit_battle_lifecycle_calls": False})
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ["exit_code", "timed_out", "inputs_unchanged"]}))
    return int(result["exit_code"] != 0 or result["timed_out"] or not result["inputs_unchanged"])


if __name__ == "__main__":
    raise SystemExit(main())
