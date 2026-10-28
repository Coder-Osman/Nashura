#!/usr/bin/env python3
"""Read SharedPreferences backing-file paths from the isolated app process."""

import argparse
import json
import subprocess
from pathlib import Path


def main():
    import frida

    parser = argparse.ArgumentParser()
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", default="emulator-5580")
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit("refusing to overwrite output: " + str(args.output))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cmdline = subprocess.check_output(
        [str(args.adb), "-s", args.serial, "shell", "cat",
         "/proc/%d/cmdline" % args.pid], text=True
    ).replace("\x00", "").strip()
    if cmdline != "nullsroyale.rel.free":
        raise SystemExit("PID identity mismatch: " + repr(cmdline))

    device = frida.get_device(args.serial, timeout=15)
    process = {item.pid: item.name for item in device.enumerate_processes()}
    if args.pid not in process:
        raise SystemExit("PID absent from Frida process list")

    with args.output.open("x", encoding="utf-8") as output:
        session = None
        evidence = {
            "device": args.serial,
            "pid": args.pid,
            "process": cmdline,
            "frida_process_label": process[args.pid],
            "read_only": True,
        }
        try:
            session = device.attach(args.pid)
            script = session.create_script(args.script.read_text(encoding="utf-8"))
            script.load()
            result = script.exports_sync.inspect()
            if "inspection_error" in result:
                raise RuntimeError(result["inspection_error"])
            evidence["preferences"] = result
            print(json.dumps(result, sort_keys=True))
        except Exception as error:
            evidence["inspection_error"] = str(error)
            raise
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception as error:
                    evidence["detach_error"] = str(error)
            output.write(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
