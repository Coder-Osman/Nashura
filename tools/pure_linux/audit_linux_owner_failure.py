"""Validate the bounded owner-getter failure and its uninstrumented control."""

import argparse
import hashlib
import json
from pathlib import Path

ENGINE = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(debug, observation, debug_stdout, control, control_stdout, control_stderr):
    for result in (debug, control):
        if (result.get("status") != "BLOCKED" or result.get("timed_out") is not False or
                result.get("inputs_unchanged") is not True or result.get("resources_unchanged") is not True or
                result.get("auxiliary_unchanged") is not True or result.get("inputs_before", {}).get("libg.so") != ENGINE):
            raise ValueError("identity or postcheck incomplete")
    for text in (debug_stdout, control_stdout):
        if ("OWNER_SLOT_BEFORE_NONNULL\t0\n" not in text or "PHASE\towner_getter_enter\n" not in text or
                "PHASE\towner_getter_return\n" in text):
            raise ValueError("owner call boundary mismatch")
    targets = {row["name"]: row for row in observation.get("targets", [])}
    ctor = targets.get("owner_constructor", {})
    db = targets.get("engine_database_init", {})
    registers = observation.get("stop_registers", {})
    if (debug.get("exit_code") != 1 or control.get("exit_code") != -11 or
            "Fatal signal 11 (SIGSEGV)" not in control_stderr or "fault addr 0x0" not in control_stderr or
            observation.get("error") is not None or observation.get("constructors_covered") is not False or
            ctor.get("armed") is not True or ctor.get("calls") != 1 or
            db.get("armed") is not True or db.get("calls") != 0 or
            registers.get("x0") != 0 or registers.get("pc", 0) - ctor.get("base", 0) != 0x17d8b14 or
            registers.get("x30", 0) - ctor.get("base", 0) != 0x140b320 or
            "SIGSEGV" not in observation.get("final_stop", "")):
        raise ValueError("failure observation mismatch")
    return {"status": "RUNTIME_OBSERVED_LIMITED", "engine_sha256": ENGINE,
            "debugger_exit_code": 1, "control_exit_code": -11, "constructor_calls": 1,
            "database_init_calls_post_load": 0, "fault_pc_offset": "0x17d8b14",
            "return_pc_offset": "0x140b320", "fault_x0_zero": True,
            "catalog_ready_pass": False,
            "limitations": ["constructor-before-load observation is not covered",
                            "control is traced by strace, not a completely uninstrumented run",
                            "runtime object and complete JNI semantics are unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug-dir", type=Path, required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    debug = json.loads((args.debug_dir / "result.json").read_text())
    control = json.loads((args.control_dir / "result.json").read_text())
    for result, directory, names in ((debug, args.debug_dir, ("stdout.log", "observation.json")),
                                     (control, args.control_dir, ("stdout.log", "stderr.log"))):
        for name in names:
            if result.get("outputs", {}).get(name) != sha(directory / name):
                raise ValueError("output identity mismatch: " + name)
    observation = json.loads((args.debug_dir / "observation.json").read_text())
    summary = compare(debug, observation, (args.debug_dir / "stdout.log").read_text(), control,
                      (args.control_dir / "stdout.log").read_text(), (args.control_dir / "stderr.log").read_text())
    summary.update(debug_result_sha256=sha(args.debug_dir / "result.json"),
                   control_result_sha256=sha(args.control_dir / "result.json"))
    with args.output.open("x") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
