"""GDB script for an owned probe child stopped at its post-load SIGSTOP marker.

The target JSON and output paths are supplied through NASHURA_GDB_TARGETS and
NASHURA_GDB_OUTPUT. Counts cover only explicitly armed addresses after dlopen.
"""
import hashlib
import json
import os
from pathlib import Path

import gdb
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from observed_string import read_string
from native_observation_scopes import ObservationScopes
from catalog_table_counts import snapshot as snapshot_table_counts


targets = json.loads(Path(os.environ["NASHURA_GDB_TARGETS"]).read_text())
output = Path(os.environ["NASHURA_GDB_OUTPUT"])
if output.exists():
    raise RuntimeError("observation output already exists")
record = {"coverage": "explicit addresses after load marker only",
          "constructors_covered": False, "events": [], "targets": [],
          "exit_code": None, "stop": None}
scopes = ObservationScopes()
scope_breakpoints = {}


class Observe(gdb.Breakpoint):
    def __init__(self, row, address):
        super().__init__("*0x%x" % address, internal=True)
        self.row = row

    def stop(self):
        frame = gdb.newest_frame()
        thread = gdb.selected_thread()
        thread_id = tuple(thread.ptid)
        scope = self.row.get("scope")
        if scope and not scopes.contains(scope, thread_id):
            return False
        self.row["calls"] += 1
        event = {"name": self.row["name"], "thread": list(thread.ptid),
                                  "pc": int(frame.read_register("pc")),
                                  "lr": int(frame.read_register("x30")),
                                  "arguments": [int(frame.read_register("x%d" % index))
                                                for index in range(8)]}
        record["events"].append(event)
        counts_register = self.row.get("table_counts_register")
        if counts_register:
            event["table_counts_before"] = snapshot_table_counts(gdb.selected_inferior().read_memory,
                                                                   int(frame.read_register(counts_register)))
        if self.row.get("context_slot_b60"):
            context = int(frame.read_register("x0")) & ((1 << 56) - 1)
            slot = bytes(gdb.selected_inferior().read_memory(context + 0xb60, 8))
            if len(slot) != 8:
                raise ValueError("short context slot read")
            event["context_slot_b60_nonnull"] = int.from_bytes(slot, "little") != 0
        for specification in self.row.get("string_arguments", []):
            register = specification["register"]
            if register not in {"x%d" % index for index in range(8)}:
                raise ValueError("unsupported observation register")
            try:
                value = read_string(gdb.selected_inferior().read_memory,
                                    int(frame.read_register(register)),
                                    specification["encoding"], specification.get("limit", 4096))
            except Exception as error:
                value = {"error": str(error)}
            event.setdefault("strings", {})[register] = value
        for slot in self.row.get("pointer_slots", []):
            address = self.row["base"] + slot["offset"]
            event.setdefault("pointer_slots", {})[slot["name"]] = int.from_bytes(
                gdb.selected_inferior().read_memory(address, 8).tobytes(), "little")
        scope_start = self.row.get("scope_start")
        if scope_start:
            scopes.enter(scope_start, thread_id)
            for breakpoint in scope_breakpoints[scope_start]:
                breakpoint.enabled = True
        if not self.row.get("site_only", False):
            Returned(event, scope_start, counts_register)
        return False


class Returned(gdb.Breakpoint):
    def __init__(self, event, scope_start=None, counts_register=None):
        super().__init__("*0x%x" % event["lr"], internal=True)
        self.event = event
        self.thread_id = gdb.selected_thread().ptid
        self.stack = int(gdb.newest_frame().read_register("sp"))
        self.scope_start = scope_start
        self.counts_context = event["arguments"][0] if counts_register else None

    def stop(self):
        frame = gdb.newest_frame()
        if gdb.selected_thread().ptid != self.thread_id or int(frame.read_register("sp")) != self.stack:
            return False
        self.event["return_x0"] = int(frame.read_register("x0"))
        if self.counts_context is not None:
            self.event["table_counts_after"] = snapshot_table_counts(gdb.selected_inferior().read_memory,
                                                                      self.counts_context)
        if self.scope_start:
            scopes.leave(self.scope_start, tuple(self.thread_id))
            if not scopes.any(self.scope_start):
                for breakpoint in scope_breakpoints[self.scope_start]:
                    breakpoint.enabled = False
        self.enabled = False
        return False


def exited(event):
    record["exit_code"] = getattr(event, "exit_code", None)


gdb.events.exited.connect(exited)
try:
    gdb.execute("set pagination off")
    gdb.execute("set confirm off")
    inferior = gdb.selected_inferior()
    owned_pid = int(os.environ["NASHURA_GDB_OWNED_PID"])
    if not owned_pid or inferior.pid != owned_pid:
        raise RuntimeError("refusing observation of an unowned inferior")
    stop = gdb.execute("info program", to_string=True)
    record["stop"] = stop
    mappings = []
    for line in Path("/proc/%d/maps" % inferior.pid).read_text().splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6 or not fields[5].startswith("/") or int(fields[2], 16) != 0:
            continue
        start = int(fields[0].split("-")[0], 16)
        mappings.append((Path(fields[5]), start - int(fields[2], 16)))
    names = [target.get("name") for target in targets]
    if len(names) != len(set(names)):
        raise RuntimeError("duplicate observation target")
    scope_names = {target["scope_start"] for target in targets if target.get("scope_start")}
    if len(scope_names) != sum(bool(target.get("scope_start")) for target in targets):
        raise RuntimeError("duplicate scope start")
    scope_breakpoints = {name: [] for name in scope_names}
    for target in targets:
        matches = {(path, base) for path, base in mappings if path.name == target["module"]}
        if len(matches) != 1:
            raise RuntimeError("ambiguous module: " + target["module"])
        path, base = matches.pop()
        if hashlib.sha256(path.read_bytes()).hexdigest() != target["sha256"]:
            raise RuntimeError("module identity changed")
        row = {"name": target["name"], "module": target["module"],
               "offset": target["offset"], "calls": 0, "armed": False,
               "base": base, "pointer_slots": target.get("pointer_slots", [])}
        row["site_only"] = target.get("site_only", False)
        if type(row["site_only"]) is not bool:
            raise RuntimeError("site_only must be boolean")
        row["string_arguments"] = target.get("string_arguments", [])
        row["scope"] = target.get("scope")
        row["scope_start"] = target.get("scope_start")
        row["table_counts_register"] = target.get("table_counts_register")
        row["context_slot_b60"] = target.get("context_slot_b60", False)
        if type(row["context_slot_b60"]) is not bool:
            raise RuntimeError("context slot profile must be boolean")
        if row["table_counts_register"] not in (None, "x0") or (row["table_counts_register"] and row["site_only"]):
            raise RuntimeError("table count observation requires x0 and a return breakpoint")
        if row["scope"] and row["scope"] not in scope_names:
            raise RuntimeError("unknown observation scope")
        if row["scope_start"] and row["site_only"]:
            raise RuntimeError("scope start requires a return breakpoint")
        record["targets"].append(row)
        protected = json.loads(os.environ.get("NASHURA_GDB_GUARD_RANGES", "{}"))
        if any(start <= target["offset"] < start + 16 for start in protected.get(target["module"], [])):
            raise RuntimeError("breakpoint overlaps guarded entry bytes")
        breakpoint = Observe(row, base + target["offset"])
        if row["scope"]:
            breakpoint.enabled = False
            scope_breakpoints[row["scope"]].append(breakpoint)
        row["armed"] = True
    gdb.execute("handle SIGSTOP nostop noprint nopass")
    gdb.execute("signal 0")
    if record["exit_code"] is None and gdb.selected_inferior().pid:
        record["final_stop"] = gdb.execute("info program", to_string=True)
        record["backtrace"] = gdb.execute("thread apply all bt 24", to_string=True)
        frame = gdb.newest_frame()
        record["stop_registers"] = {name: int(frame.read_register(name))
                                    for name in ["pc", "x30", "x0", "x1", "x2", "x3"]}
except Exception as error:
    record["error"] = str(error)
finally:
    record["active_scopes"] = len(scopes.active)
    if gdb.selected_inferior().pid and gdb.selected_inferior().pid == int(os.environ.get("NASHURA_GDB_OWNED_PID", "0")):
        gdb.execute("kill")
    with output.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
if record.get("error") or record["exit_code"] != 0:
    gdb.execute("quit 1")
