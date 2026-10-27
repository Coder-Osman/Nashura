"""GDB script for a new probe child stopped at its post-load SIGTRAP marker.

The target JSON and output paths are supplied through NASHURA_GDB_TARGETS and
NASHURA_GDB_OUTPUT. Counts cover only explicitly armed addresses after dlopen.
"""
import hashlib
import json
import os
from pathlib import Path

import gdb


targets = json.loads(Path(os.environ["NASHURA_GDB_TARGETS"]).read_text())
output = Path(os.environ["NASHURA_GDB_OUTPUT"])
if output.exists():
    raise RuntimeError("observation output already exists")
record = {"coverage": "explicit addresses after load marker only",
          "constructors_covered": False, "events": [], "targets": [],
          "exit_code": None, "stop": None}


class Observe(gdb.Breakpoint):
    def __init__(self, row, address):
        super().__init__("*0x%x" % address, internal=True)
        self.row = row

    def stop(self):
        self.row["calls"] += 1
        frame = gdb.newest_frame()
        thread = gdb.selected_thread()
        record["events"].append({"name": self.row["name"], "thread": list(thread.ptid),
                                  "pc": int(frame.read_register("pc")),
                                  "lr": int(frame.read_register("x30")),
                                  "arguments": [int(frame.read_register("x%d" % index))
                                                for index in range(4)]})
        return False


def exited(event):
    record["exit_code"] = getattr(event, "exit_code", None)


gdb.events.exited.connect(exited)
try:
    gdb.execute("set pagination off")
    gdb.execute("set confirm off")
    gdb.execute("run")
    inferior = gdb.selected_inferior()
    if not inferior.pid:
        raise RuntimeError("probe exited before observation marker")
    stop = gdb.execute("info program", to_string=True)
    record["stop"] = stop
    if "SIGTRAP" not in stop:
        raise RuntimeError("expected owned probe SIGTRAP marker")
    mappings = []
    for line in Path("/proc/%d/maps" % inferior.pid).read_text().splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6 or not fields[5].startswith("/") or int(fields[2], 16) != 0:
            continue
        start = int(fields[0].split("-")[0], 16)
        mappings.append((Path(fields[5]), start - int(fields[2], 16)))
    for target in targets:
        matches = {(path, base) for path, base in mappings if path.name == target["module"]}
        if len(matches) != 1:
            raise RuntimeError("ambiguous module: " + target["module"])
        path, base = matches.pop()
        if hashlib.sha256(path.read_bytes()).hexdigest() != target["sha256"]:
            raise RuntimeError("module identity changed")
        row = {"name": target["name"], "module": target["module"],
               "offset": target["offset"], "calls": 0, "armed": False}
        record["targets"].append(row)
        Observe(row, base + target["offset"])
        row["armed"] = True
    gdb.execute("continue")
except Exception as error:
    record["error"] = str(error)
finally:
    with output.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
if record.get("error") or record["exit_code"] != 0:
    gdb.execute("quit 1")
