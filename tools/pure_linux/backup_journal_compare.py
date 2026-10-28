"""Compare an isolated C journal append with real framework probe observations."""

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess


VALUES = ["nullsroyale.rel.free", "nullsroyale.rel.free", "", "zero\0tail",
          "中文", "😀", "\ud800", "\udfff"]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def events(path):
    return [json.loads(line.split("\t", 1)[1]) for line in path.read_text().splitlines()
            if line.startswith("JOURNAL_EVENT\t")]


def observed(path, phase):
    if phase not in {"prepare", "reopen"}:
        raise ValueError("unknown phase")
    rows = events(path)
    identities = [row for row in rows if row.get("case") == "identity"]
    if len(identities) != 1 or identities[0].get("class") != "com.android.server.backup.DataChangedJournal":
        raise ValueError("framework class identity missing")
    if any(type(identities[0].get(key)) is not int or identities[0][key] <= 0 for key in ("pid", "tid")):
        raise ValueError("framework process identity missing")
    if phase == "prepare":
        appends = [row for row in rows if row.get("case") == "append"]
        if [row.get("index") for row in appends] != list(range(len(VALUES))):
            raise ValueError("complete ordered framework appends required")
        if any(len(bytes.fromhex(row["hex"])) != row["length"] for row in appends):
            raise ValueError("framework length mismatch")
        expected = {"oversize": "java.io.UTFDataFormatException",
                    "missing_parent": "java.io.IOException", "file_as_parent": "java.io.IOException",
                    "directory_as_journal": "java.io.FileNotFoundException"}
        failures = [row for row in rows if "exception" in row]
        if len(failures) != len(expected) or {row["case"]: row["exception"] for row in failures} != expected:
            raise ValueError("framework exception coverage missing")
        if len(rows) != 14 or rows[-1].get("case") != "prepared" or rows[-1].get("records") != 8:
            raise ValueError("framework completion missing")
        return [bytes.fromhex(row["hex"]) for row in appends]
    reopens = [row for row in rows if row.get("case") == "reopened"]
    if len(rows) != 2 or len(reopens) != 1 or reopens[0].get("records") != 9:
        raise ValueError("framework reopen completion missing")
    payload = bytes.fromhex(reopens[0]["hex"])
    if hashlib.sha256(payload).hexdigest() != reopens[0]["sha256"]:
        raise ValueError("framework reopen digest mismatch")
    return payload


def compare(library, reference, directory, phase, probe=None, linker=None):
    if library is not None:
        function = ctypes.CDLL(str(library.resolve()), use_errno=True).backup_journal_append_utf16
        function.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint16), ctypes.c_size_t]
        function.restype = ctypes.c_int

    def append(path, value):
        if probe is not None:
            payload = value.encode("utf-16-be", errors="surrogatepass").hex()
            command = ([str(linker.resolve())] if linker else []) + [str(probe.resolve()), str(path), payload]
            result = subprocess.run(command, capture_output=True, timeout=15)
            if result.returncode:
                raise RuntimeError("native probe exit " + str(result.returncode) + ": "
                                   + result.stderr.decode(errors="replace")[:2000])
            record = json.loads(result.stdout)
            if result.stderr or set(record) != {"result", "errno"}:
                raise ValueError("unexpected native probe output")
            return record["result"], record["errno"]
        payload = value.encode("utf-16-le", errors="surrogatepass")
        units = (ctypes.c_uint16 * (len(payload) // 2)).from_buffer_copy(payload)
        ctypes.set_errno(0)
        result = function(os.fsencode(path), units, len(units))
        return result, ctypes.get_errno()

    path = directory / "journal"
    checks = []
    if phase == "prepare":
        directory.mkdir(mode=0o700, exist_ok=False)
        for index, value in enumerate(VALUES):
            if append(path, value)[0] != 0 or path.read_bytes() != reference[index]:
                raise ValueError("framework append mismatch at " + str(index))
            checks.append("append-" + str(index))
        if append(path, "中" * 21846) != (-1, errno.EOVERFLOW) or path.read_bytes() != reference[-1]:
            raise ValueError("overflow preservation mismatch")
        regular = directory / "regular-parent"
        regular.write_bytes(b"owned fixture")
        if append(directory / "missing" / "journal", "x") != (-1, errno.ENOENT):
            raise ValueError("missing-parent mismatch")
        if append(regular / "journal", "x") != (-1, errno.ENOTDIR):
            raise ValueError("file-parent mismatch")
        if append(directory, "x") != (-1, errno.EISDIR):
            raise ValueError("directory-journal mismatch")
        checks.extend(["overflow-preserved", "missing-parent", "file-parent", "directory-journal"])
    else:
        if not path.is_file() or not reference.endswith(bytes.fromhex("000872656f70656e6564")):
            raise ValueError("prepared file and expected suffix required")
        if path.read_bytes() != reference[:-10]:
            raise ValueError("cross-process input mismatch")
        if append(path, "reopened")[0] != 0 or path.read_bytes() != reference:
            raise ValueError("framework reopen mismatch")
        checks.append("cross-process-reopen-append")
    return {"checks": checks, "journal_sha256": digest(path), "journal_bytes": path.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("android-log", "directory", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    implementation = parser.add_mutually_exclusive_group(required=True)
    implementation.add_argument("--library", type=Path)
    implementation.add_argument("--probe", type=Path)
    parser.add_argument("--linker", type=Path)
    parser.add_argument("--phase", choices=("prepare", "reopen"), required=True)
    args = parser.parse_args()
    record = {"schema": "nashura.backup-journal-comparison.v1", "phase": args.phase,
              "os": platform.system(), "machine": platform.machine(), "kernel": platform.release(),
              "pid": os.getpid(), "libg_loaded": False, "backup_binding_pass": False,
              "catalog_ready_pass": False, "status": "BLOCKED"}
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        try:
            if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
                raise ValueError("ARM64 Linux runtime required")
            files = {"implementation": args.library or args.probe, "android_log": args.android_log,
                     "comparison_script": Path(__file__)}
            if args.linker:
                if not args.probe:
                    raise ValueError("linker requires native probe")
                files["linker"] = args.linker
            inputs = {name: digest(path) for name, path in files.items()}
            record["inputs"] = inputs
            record.update(compare(args.library, observed(args.android_log, args.phase), args.directory,
                                  args.phase, args.probe, args.linker))
            if inputs != {name: digest(path) for name, path in files.items()}:
                raise ValueError("input identity changed")
            record["status"] = "JOURNAL_APPEND_PARITY_LIMITED"
        except BaseException as error:
            record["error"] = type(error).__name__ + ": " + str(error)
            raise
        finally:
            json.dump(record, stream, indent=2)
            stream.write("\n")


if __name__ == "__main__":
    main()
