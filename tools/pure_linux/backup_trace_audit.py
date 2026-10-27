"""Audit complete backup call scopes without publishing ART addresses or arguments."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import struct


ROOT = ("com.supercell.titan.GameApp", "requestBackup", "()V")
METHODS = (
    ROOT,
    ("android.app.backup.BackupManager", "<init>", "(Landroid/content/Context;)V"),
    ("android.app.backup.BackupManager", "checkServiceBinder", "()V"),
    ("android.app.backup.BackupManager", "dataChanged", "()V"),
    ("android.app.backup.IBackupManager$Stub", "asInterface",
     "(Landroid/os/IBinder;)Landroid/app/backup/IBackupManager;"),
    ("android.app.backup.IBackupManager$Stub$Proxy", "<init>", "(Landroid/os/IBinder;)V"),
    ("android.app.backup.IBackupManager$Stub$Proxy", "dataChanged", "(Ljava/lang/String;)V"),
    ("android.os.ServiceManager", "getService", "(Ljava/lang/String;)Landroid/os/IBinder;"),
    ("android.os.ServiceManager", "rawGetService", "(Ljava/lang/String;)Landroid/os/IBinder;"),
    ("android.os.ServiceManager", "getIServiceManager", "()Landroid/os/IServiceManager;"),
    ("android.os.ServiceManagerProxy", "getService", "(Ljava/lang/String;)Landroid/os/IBinder;"),
    ("android.os.IServiceManager$Stub$Proxy", "checkService",
     "(Ljava/lang/String;)Landroid/os/IBinder;"),
    ("android.os.BinderProxy", "queryLocalInterface", "(Ljava/lang/String;)Landroid/os/IInterface;"),
    ("android.os.BinderProxy", "transact", "(ILandroid/os/Parcel;Landroid/os/Parcel;I)Z"),
    ("android.os.BinderProxy", "transactNative", "(ILandroid/os/Parcel;Landroid/os/Parcel;I)Z"),
    ("android.app.backup.IBackupManager$Stub", "getDefaultImpl",
     "()Landroid/app/backup/IBackupManager;"),
    ("android.os.IServiceManager$Stub", "getDefaultImpl", "()Landroid/os/IServiceManager;"),
    ("android.os.Parcel", "readException", "()V"),
    ("android.os.Parcel", "readExceptionCode", "()I"),
    ("android.os.Parcel", "recycle", "()V"),
    ("android.util.Log", "d", "(Ljava/lang/String;Ljava/lang/String;)I"),
    ("android.util.Log", "e", "(Ljava/lang/String;Ljava/lang/String;Ljava/lang/Throwable;)I"),
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def label(method):
    return ".".join(method[:2]) + method[2]


def parse(raw):
    boundary = raw.find(b"*end\n")
    require(boundary >= 0, "ART text boundary missing")
    lines = raw[:boundary].decode("utf-8").splitlines()
    require(lines[:2] == ["*version", "3"], "unsupported ART version")
    require(lines.count("*methods") == 1, "ART method section missing or duplicated")
    metadata = {}
    for line in lines[2:lines.index("*methods")]:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        require(key not in metadata, "duplicate ART metadata")
        metadata[key] = value
    require(metadata.get("data-file-overflow") == "false" and metadata.get("clock") == "dual",
            "overflowed trace or unsupported clock")
    methods, duplicate_rows = {}, 0
    for line in lines[lines.index("*methods") + 1:]:
        fields = line.split("\t")
        require(len(fields) >= 4, "invalid ART method row")
        identifier = int(fields[0], 0)
        require(0 <= identifier <= 0xffffffff and identifier & 3 == 0, "unaligned ART method")
        value = tuple(fields[1:])
        if identifier in methods:
            require(methods[identifier] == value, "conflicting ART method metadata")
            duplicate_rows += 1
        methods[identifier] = value
    require(ROOT in {value[:3] for value in methods.values()}, "backup root metadata absent")
    binary = raw[boundary + 5:]
    require(len(binary) >= 32, "short ART binary header")
    magic, version, offset, _, size = struct.unpack_from("<4sHHQH", binary)
    require((magic, version, offset, size) == (b"SLOW", 3, 32, 14), "unsupported ART layout")
    require((len(binary) - offset) % size == 0, "partial ART record")
    records = (len(binary) - offset) // size
    require(records == int(metadata.get("num-method-calls", -1)), "ART record count differs")
    selected = set(METHODS)
    global_counts, scoped_counts = Counter(), Counter()
    unknown, scoped_unknown = Counter(), Counter()
    active, roots, threads = {}, [], {}
    for sequence, position in enumerate(range(offset, len(binary), size)):
        thread, encoded, _, _ = struct.unpack_from("<HIII", binary, position)
        identifier, action = encoded & ~3, encoded & 3
        require(thread > 0 and action != 3, "invalid ART thread or action")
        method = methods.get(identifier, ())[:3]
        event = ("entered", "returned", "unwound")[action]
        if not method:
            unknown[identifier] += 1
        if method in selected:
            global_counts[(label(method), event)] += 1
        if method == ROOT and action == 0:
            require(thread not in active, "nested backup root")
            threads.setdefault(thread, len(threads))
            root = {"index": len(roots), "thread_alias": threads[thread],
                    "begin_sequence": sequence, "calls": [], "unknown_records": 0,
                    "nested_entries": 0, "maximum_depth": 0}
            roots.append(root)
            active[thread] = (root, [])
        if thread not in active:
            continue
        root, stack = active[thread]
        if not method:
            root["unknown_records"] += 1
            scoped_unknown[identifier] += 1
        if method in selected:
            scoped_counts[(label(method), event)] += 1
        if action == 0:
            parent = next((row[1] for row in reversed(stack) if row[1] is not None), None)
            call = None
            if method in selected:
                call = {"method": label(method), "begin_sequence": sequence,
                        "nearest_selected_parent": parent["method"] if parent else None,
                        "depth": len(stack)}
                root["calls"].append(call)
            stack.append((identifier, call))
            root["nested_entries"] += 1
            root["maximum_depth"] = max(root["maximum_depth"], len(stack))
        else:
            require(stack and stack[-1][0] == identifier, "backup scope stack mismatch")
            call = stack.pop()[1]
            if call is not None:
                call.update(end_sequence=sequence, completion=event)
            if not stack:
                require(method == ROOT, "backup scope closed by another method")
                root.update(end_sequence=sequence, completion=event)
                del active[thread]
    require(not active, "incomplete backup root at trace boundary")
    require(roots, "no backup root events observed")

    def counts(counter):
        return {label(method): {event: counter[(label(method), event)]
                for event in ("entered", "returned", "unwound")} for method in METHODS}

    present = {value[:3] for value in methods.values()}
    coverage = {label(method): {"metadata_present": method in present,
                "scope_events": sum(scoped_counts[(label(method), event)]
                                    for event in ("entered", "returned", "unwound"))}
                for method in METHODS}

    return {"schema": "nashura.backup-trace-audit.v1", "status": "RUNTIME_OBSERVED",
            "trace_sha256": hashlib.sha256(raw).hexdigest(), "records": records,
            "identical_duplicate_metadata_rows": duplicate_rows,
            "unknown_method_count": len(unknown), "unknown_records": sum(unknown.values()),
            "scoped_unknown_method_count": len(scoped_unknown),
            "scoped_unknown_records": sum(scoped_unknown.values()),
            "counts_global": counts(global_counts), "counts_scoped": counts(scoped_counts),
            "method_coverage": coverage,
            "roots": roots, "root_threads": len(threads), "scope_stacks_balanced": True,
            "coverage": "complete nested records inside requestBackup only; outside scopes may be truncated",
            "arguments_and_return_values": "NOT_RECORDED",
            "backup_service_delivery": "UNRESOLVED", "current_linux_binding": "UNRESOLVED",
            "catalog_ready_pass": False}


def audit(trace, expected_sha256, output):
    raw = trace.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, "trace identity mismatch")
    report = parse(raw)
    require(trace.read_bytes() == raw, "trace changed during audit")
    with output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--trace-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    audit(args.trace, args.trace_sha256, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
