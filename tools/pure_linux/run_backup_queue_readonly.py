"""Inspect the isolated system_server backup queue without invoking dataChanged."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def identity(adb, serial, pid):
    result = subprocess.run(
        [str(adb), "-s", serial, "shell", "cat", "/proc/%d/cmdline" % pid],
        check=True, capture_output=True, timeout=15,
    ).stdout.rstrip(b"\x00\r\n")
    if result != b"system_server":
        raise ValueError("system_server PID identity mismatch")
    return result.decode("ascii")


def inspect(adb, serial, pid, script_path, output_path, frida):
    source = script_path.read_bytes()
    record = {"script_sha256": hashlib.sha256(source).hexdigest(),
              "serial": serial, "pid": pid, "read_only": True}
    with os.fdopen(os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        session = None
        try:
            record["process"] = identity(adb, serial, pid)
            device = frida.get_device(serial, timeout=15)
            names = {process.pid: process.name for process in device.enumerate_processes()}
            if names.get(pid) != "system_server":
                raise ValueError("Frida process identity mismatch")
            session = device.attach(pid)
            script = session.create_script(source.decode("utf-8"))
            script.load()
            record["inspection"] = script.exports_sync.inspect()
            record["process_after"] = identity(adb, serial, pid)
            if script_path.read_bytes() != source:
                raise ValueError("script identity changed")
        except BaseException as error:
            record["error"] = str(error)
            raise
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception as error:
                    record["detach_error"] = str(error)
            json.dump(record, output, indent=2)
            output.write("\n")
    return record


def main():
    import frida

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inspect(args.adb, args.serial, args.pid, args.script, args.output, frida)
    print(json.dumps({"status": "RUNTIME_OBSERVED", "read_only": True}))


if __name__ == "__main__":
    main()
