"""Validate private paired E1 attachments, keeping diagnostics separate from battle coverage."""

import hashlib
import json
from pathlib import Path

FMOD_SYMBOLS = {"AAssetManager_fromJava", "AAssetManager_open", "AAsset_read",
                "AAsset_seek", "AAsset_close", "AAsset_getLength"}
INPUT_HASHES = {
    "base.apk": "f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b",
    "libg.so": "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783",
    "libcrprobe.so": "84cddf82e866540ff33d4b5f0c6b5b535c8485182e4cc84961028c7a56e65ec4",
    "libfmod.so": "299d2a6b03ec865cd9234f4f7484fb7e74928d0180dc6c4b455a8d2b5b54dd45",
}
SEMANTIC_CHECKS = ("bytes", "read", "seek", "length", "remaining", "descriptor", "missing",
                   "namespace_priority", "fallback", "snapshot_restore", "reset",
                   "identities_unchanged", "fmod_binding", "fmod_files", "damaged_data",
                   "provider_identity", "stock_trajectory")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_attachments(manifest, directory):
    require(manifest.get("schema") == "e1-semantics.v2", "hashed semantics manifest required")
    result, hashes = {}, {}
    for name, entry in manifest.get("attachments", {}).items():
        require(entry.get("format") in ("json", "text"), "unknown attachment format")
        path = directory / entry["path"]
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        require(digest == entry["sha256"], "attachment hash differs: " + name)
        result[name] = json.loads(content) if entry["format"] == "json" else content.decode("utf-8")
        hashes[name] = digest
    return result, hashes


def routing_events(row, roots):
    receivers = {roots["filesystem"]: "filesystem", roots["apk"]["object"]: "apk",
                 roots["disk"]["object"]: "update"}
    receivers.update({entry["object"]: entry["namespace"] for entry in roots["providers"]})
    events = []
    for event in row["events"]:
        require("path_error" not in event, "unreadable engine resource path")
        require(event["receiver"] in receivers, "unknown engine provider receiver")
        value = int(event["result"], 16)
        result = value if event["name"].endswith("exists") else bool(value)
        caller = event["caller"] if event["name"] not in ("filesystem_resolve", "filesystem_open") else "diagnostic-helper"
        events.append((event["name"], receivers[event["receiver"]], event["path"], caller, result))
    return events


def verify_namespace(stock, replacement, stock_roots, replacement_roots, fixture):
    expected = (
        [("apk_only_or_missing", path, length) for path, length in [
            ("build.tag", 8), ("data:/build.tag", 8), ("update:/build.tag", -1),
            ("image/icons/Battlefy_logo.png", 1641), ("data:/image/icons/Battlefy_logo.png", 1641),
            (fixture + "/missing", -1), ("data:/" + fixture + "/missing", -1),
            ("update:/" + fixture + "/missing", -1)]]
        + [("update_present", path, length) for path, length in [
            ("build.tag", 26), ("data:/build.tag", 8), ("update:/build.tag", 26),
            (fixture + "/only.txt", 15), ("data:/" + fixture + "/only.txt", -1),
            ("update:/" + fixture + "/only.txt", 15), (fixture + "/other/build.tag", 18),
            ("data:/" + fixture + "/other/build.tag", -1), (fixture + "/malformed.csv", 22),
            ("data:/./build.tag", 8), ("data:/../build.tag", -1), ("data://build.tag", 8),
            ("unknown:/build.tag", -1)]]
        + [("update_removed_fallback", path, length) for path, length in [
            ("build.tag", 8), ("data:/build.tag", 8), ("update:/build.tag", -1)]]
    )
    require(len(stock) == len(replacement) == len(expected), "incomplete 24-case engine matrix")
    observed_names = set()
    for first, second, (stage, path, length) in zip(stock, replacement, expected):
        for row in (first, second):
            require(row["stage"] == stage and row["result"]["path"] == path, "matrix order or case differs")
            require(row["result"]["read_result"] == length, "unexpected native resource result: " + path)
            require("resolve_error" not in row["result"], "native resolver failed")
            require(len(row["result"]["operations"]) == 24, "seek/EOF diagnostic missing")
            require(row["result"]["operation_status"] == (0 if length >= 0 else -1), "native file exercise failed")
            require(row["result"]["bytes"] is None if length < 0 else len(row["result"]["bytes"]) == length,
                    "resource byte count differs")
        first, second = first["result"], second["result"]
        for key in ("resolved_path", "read_result", "operation_status", "operations", "details", "bytes"):
            require(first[key] == second[key], "paired namespace mismatch: " + path + ":" + key)
        first_events, second_events = routing_events(first, stock_roots), routing_events(second, replacement_roots)
        require(first_events == second_events and first_events, "engine routing/exists differs: " + path)
        observed_names.update(event[0] for event in first_events)
        if path == "build.tag":
            namespace = "update" if stage == "update_present" else "apk"
            require(any(event[1] == namespace and event[0].endswith("open") and event[-1]
                        for event in first_events), "priority/fallback lacks successful native open")
    require({"filesystem_resolve", "filesystem_open", "apk_exists", "apk_open", "disk_exists", "disk_open"}
            <= observed_names, "engine entry coverage incomplete")


def verify_fmod_binding(binding):
    require(binding["schema"] == "e1-fmod-binding.v1" and binding["installed"] is True
            and binding["mode"] == "redirect" and binding["errors"] == [], "FMOD installation incomplete")
    resolved = [row for row in binding["events"] if row["kind"] == "resolved_binding"]
    redirects = [row for row in binding["events"] if row["kind"] == "redirected_binding"]
    require(len(resolved) == len(redirects) == 6, "six FMOD resolver slots required")
    require({row["name"] for row in resolved} == {row["name"] for row in redirects} == FMOD_SYMBOLS,
            "FMOD resolver symbols differ")
    require(all(row["verified"] is True and row["slot_verified"] is True
                and row["actual_target"].startswith("libandroid.so+") for row in resolved), "FMOD original slots unverified")
    require(all(row["verified"] is True and row["target"].startswith("libe1provider.so+") for row in redirects),
            "FMOD redirect target unverified")
    imports = [row for row in binding["events"] if row["kind"] == "resolver_import"]
    require({row["name"] for row in imports} == {"dlopen", "dlsym"}
            and all(row["actual_target"].startswith("libdl.so+") for row in imports), "FMOD resolver imports unknown")
    dynamic = [row for row in binding["events"] if row["kind"] == "resolver_call"
               and row["name"] == "dlsym" and row.get("request") in FMOD_SYMBOLS
               and row["caller"].startswith("libfmod.so+")]
    require(len(dynamic) == 6 and {row["request"] for row in dynamic} == FMOD_SYMBOLS
            and all(row["actual_target"].startswith("libdl.so+")
                    and row["resolved_target"].startswith("libandroid.so+") for row in dynamic),
            "FMOD actual dlsym path unobserved")
    loaded = [row for row in binding["events"] if row["kind"] == "resolver_call"
              and row["name"] == "dlopen" and row["caller"].startswith("libfmod.so+")
              and row.get("request") == "libandroid.so" and int(row["result"], 16) != 0]
    require(loaded and all(any(row["argument0"] == request["result"] for request in loaded)
                           for row in dynamic), "FMOD Android library handle propagation unobserved")
    initialized = [row for row in binding["events"] if row["kind"] == "initialize_leave"]
    require(len(initialized) == 1 and initialized[0]["result"] == 0, "FMOD initialization failed")
    initialized = initialized[0]
    final_targets = {row["name"]: row["target"] for row in initialized["bindings"]}
    require(all(final_targets.get(row["name"]) == row["target"] for row in redirects), "FMOD saved slots differ")
    calls = [row for row in binding["resource_calls"]
             if row["name"] == "AAssetManager_fromJava" and row["caller"].startswith("libfmod.so+")]
    require(len(calls) == 1 and calls[0]["count"] == 1 and calls[0]["thread"] > 0
            and calls[0]["phase"] and calls[0]["target"] == final_targets["AAssetManager_fromJava"],
            "FMOD direct manager call missing")
    sample = calls[0]["samples"][0]
    require(int(sample["result"], 16) != 0 and sample["result"] == initialized["manager"]
            and sample["argument1"] == initialized["java_global_ref"], "FMOD manager propagation differs")


def verify_fmod_files(stock, replacement):
    require(len(stock) == len(replacement) == 3, "FMOD file cases incomplete")
    names = set()
    for first, second in zip(stock, replacement):
        require({key: value for key, value in first.items() if key != "events"}
                == {key: value for key, value in second.items() if key != "events"}, "FMOD file behavior differs")
        require(first["events"] and second["events"], "FMOD native resource calls missing")
        require(all(event["caller"].startswith("libfmod.so+") for event in first["events"] + second["events"]),
                "FMOD OS file diagnostic bypassed FMOD")
        require(all(event["target"].startswith("libandroid.so+") for event in first["events"]), "FMOD stock target differs")
        require(all(event["target"].startswith("libe1provider.so+") for event in second["events"]), "FMOD platform resource event remains")
        names.update(event["name"] for event in second["events"])
    require(FMOD_SYMBOLS - {"AAssetManager_fromJava"} <= names, "FMOD file operation coverage incomplete")
    require([row["length"] for row in stock] == [8, 1641, 0]
            and [row["open_result"] for row in stock] == [0, 0, 18], "FMOD file cases differ")


def verify_controls(stock, replacement):
    def relevant(rows):
        result = []
        for row in rows:
            if row["phase"] == "cold_ready_wait":
                continue
            response = dict(row["response"])
            if row["command"].startswith("standalone-trace "):
                require(bool(response.pop("snapshot_directory", None)), "snapshot output directory missing")
            result.append((row["phase"], row["command"], response))
        return result
    require(relevant(stock) == relevant(replacement), "native lifecycle responses differ")
    responses = [row[2] for row in relevant(stock)]
    require(all(row.get("ok") is True for row in responses), "native control failed")
    snapshots = [row[2] for row in relevant(stock) if row[1] == "snapshot-create"]
    require(len(snapshots) == 3 and snapshots[1]["tick"] == snapshots[2]["tick"] == 21
            and snapshots[1]["digest"] == snapshots[2]["digest"]
            and snapshots[1]["bytes"] == snapshots[2]["bytes"] > 0, "snapshot replay differs")
    commands = [row[1] for row in relevant(stock)]
    require("restore 1" in commands and all("release-snapshot " + str(handle) in commands for handle in (1, 2, 3)),
            "restore or release evidence missing")
    final = stock[-1]
    require(final["phase"] == "final_reset_state" and final["response"]["tick"] == 0
            and final["response"]["finalized"] is False and final["response"]["snapshotHandles"] == 0
            and final["response"]["queuedCommands"] == 0, "final reset incomplete")


def verify_damaged_data(boundary):
    require(boundary["schema"] == "e1-native-asset-boundary.v2" and boundary["cookie"] > 0,
            "real Android archive diagnostic missing")
    names = {"stored", "deflated", "crc_bad_stored", "deflate_bad", "missing", "../stored", "./stored", "/stored"}
    rows = {(row["name"], row["mode"]): row for row in boundary["rows"]}
    require(len(boundary["rows"]) == len(rows) == 32
            and set(rows) == {(name, mode) for name in names for mode in range(4)},
        "archive boundary cases missing or duplicated")
    for (name, mode), row in rows.items():
        original = row["stock"]
        if name not in ("stored", "deflated", "crc_bad_stored", "deflate_bad"):
            require(original == dict(opened=False), "Android invalid path behavior differs")
            continue
        damaged = name == "deflate_bad"
        require(original["opened"] is True and original["length"] == "240" and len(original["seeks"]) == 6
                and original["zero_read"] == (-1 if damaged else 0)
                and original["read"] == (-1 if damaged else 240)
                and original["remaining"] == ("240" if damaged else "0")
                and original["again"] == (-1 if damaged else 0),
                "Android boundary baseline differs: " + name)
        require(original["bytes"] is None if original["read"] < 0 else len(original["bytes"]) == original["read"],
                "Android boundary bytes missing")
        require(original["descriptor"]["opened"] is (name in ("stored", "crc_bad_stored")),
                "Android descriptor boundary differs")
    require(boundary["metrics"]["live_assets"] == "0" and int(boundary["metrics"]["failures"]) > 0,
            "intentional corruption errors must remain counted and handles closed")
    differences = [row["name"] + ":" + str(row["mode"]) for row in boundary["rows"]
                   if row["stock"] != row["replacement"]]
    require(not differences, "native damaged archive behavior differs: " + ", ".join(differences))


def verify_identities(evidence):
    for side in ("stock", "replacement"):
        before, after = (evidence[side + "_identity_" + suffix] for suffix in ("before", "after"))
        require(before == after, "input identity changed: " + side)
        rows = {Path(line.split(maxsplit=1)[1]).name: line.split()[0] for line in before.splitlines()}
        require(rows == INPUT_HASHES, "engine/APK/probe identity differs")
        before, after = (evidence[side + "_update_" + suffix] for suffix in ("before", "after"))
        require(len(before) == len(set(before)) == 990 and before == after, "update identity changed")
    require(evidence["stock_update_before"] == evidence["replacement_update_before"], "paired update inputs differ")


def verify_provider_identity(evidence):
    first, second = evidence["resource_build"], evidence["replacement_build"]
    require(first == second and set(first) == {"sha256", "source_sha256"}
            and all(len(value) == 64 and all(character in "0123456789abcdef" for character in value)
                    for value in first.values()), "full resource comparison used a different provider build")
    metrics = evidence["resource_metrics"]
    require(metrics["result"] == 0 and int(metrics["operations"]) > 0
            and metrics["failures"] == metrics["live_assets"] == "0"
            and metrics["messages"] == [], "full resource comparison failed or leaked assets")


def verify_stock_trajectory(comparison):
    require(comparison["schema"] == "e0-snapshot-comparison.v1" and comparison["status"] == "passed"
            and comparison["snapshot_count"] == 3602 and comparison["terminal_tick"] == 3601
            and comparison["injected_actions"] == 28 and comparison["total_bytes"] == 9120440
            and comparison["trajectory_sha256"] ==
                "0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5",
            "instrumented stock trajectory differs from unobserved baseline")


def verify_semantics(manifest, evidence):
    checks = dict.fromkeys(SEMANTIC_CHECKS, False)
    errors = {}
    if manifest.get("schema") != "e1-semantics.v2":
        return checks, {"manifest": "hashed semantics manifest required"}
    groups = [
        (("namespace_priority", "fallback"), lambda: verify_namespace(evidence["stock_namespace"],
            evidence["replacement_namespace"], evidence["stock_roots"], evidence["replacement_roots"], manifest["fixture"])),
        (("fmod_binding",), lambda: verify_fmod_binding(evidence["fmod_binding"])),
        (("fmod_files",), lambda: verify_fmod_files(evidence["stock_fmod_files"], evidence["replacement_fmod_files"])),
        (("snapshot_restore", "reset"), lambda: verify_controls(evidence["stock_controls"], evidence["replacement_controls"])),
        (("identities_unchanged",), lambda: verify_identities(evidence)),
        (("damaged_data",), lambda: verify_damaged_data(evidence["asset_boundary"])),
        (("provider_identity",), lambda: verify_provider_identity(evidence)),
        (("stock_trajectory",), lambda: verify_stock_trajectory(evidence["stock_comparison"])),
    ]
    for names, operation in groups:
        try:
            operation()
            checks.update(dict.fromkeys(names, True))
        except (KeyError, TypeError, ValueError, IndexError) as error:
            errors["/".join(names)] = str(error)
    try:
        lines = evidence["resource_comparison"].strip().splitlines()
        require(len(lines) == 1 and lines[0].startswith("summary\t"), "resource mismatches or missing summary")
        values = dict(item.split("=", 1) for item in lines[0].split("\t")[1:])
        require(values == dict(files="9829", bytes="849482445", failures="0", descriptors="9829", missing="2"),
                "full real resource comparison incomplete")
        for name in ("bytes", "read", "seek", "length", "remaining", "descriptor", "missing"):
            checks[name] = True
    except (KeyError, TypeError, ValueError, IndexError) as error:
        errors["resource_comparison"] = str(error)
    return checks, errors
