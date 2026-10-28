"""Audit the bounded app-version bridge against a matched Android startup."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_owner_constructor import compare as owner_compare

ENGINE = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(android, before, before_obs, after, after_obs, after_stdout):
    owner_compare(android)
    trace = android["trace"]
    if not any(row.get("label") == "app_version_wrapper_candidate" and row.get("installed") is True
               for row in trace["coverage"]):
        raise ValueError("Android version wrapper uncovered")
    events = [e for e in trace["events"] if e.get("kind") == "app_version_wrapper"]
    owner = next(e for e in trace["events"] if e.get("kind") == "engine_db_init_enter")
    if (len(events) != 1 or events[0].get("value") != "15.535.13" or
            events[0].get("owner_constructor_id") != owner["owner_constructor"]["id"] or
            events[0].get("thread") != owner.get("thread") or events[0]["sequence"] >= owner["sequence"]):
        raise ValueError("Android version observation not linked to owner")
    for result in (before, after):
        if (result.get("status") != "BLOCKED" or result.get("timed_out") is not False or
                result.get("inputs_before", {}).get("libg.so") != ENGINE or
                not all(result.get(k) is True for k in ("inputs_unchanged", "resources_unchanged", "auxiliary_unchanged"))):
            raise ValueError("Linux identity or strict gate mismatch")
    if before.get("exit_code") != 1 or after.get("exit_code") != 0 or after.get("app_version_bridge_candidate") is not True:
        raise ValueError("Linux run status mismatch")
    first = [e for e in before_obs["events"] if e.get("name") == "jni_method_candidate"]
    if (len(first) != 1 or first[0].get("strings", {}).get("x0", {}).get("bytes_hex") != "67657441707056657273696f6e" or
            before_obs.get("error") is not None or "SIGSEGV" not in before_obs.get("final_stop", "")):
        raise ValueError("original JNI call not observed")
    rows = {row["name"]: row for row in after_obs.get("targets", [])}
    if (after_obs.get("exit_code") != 0 or after_obs.get("error") is not None or
            any(rows.get(name, {}).get("armed") is not True for name in
                ("owner_constructor", "jni_method_candidate", "jni_environment_candidate")) or
            rows["owner_constructor"].get("calls") != 1 or rows["jni_method_candidate"].get("calls") != 0 or
            rows["jni_environment_candidate"].get("calls") != 0 or
            not any(e.get("name") == "owner_constructor" and "return_x0" in e for e in after_obs.get("events", []))):
        raise ValueError("Linux constructor observation incomplete")
    lines = after_stdout.splitlines()
    for line in ("APP_VERSION_BRIDGE_INSTALLED\tverified_package_version_candidate",
                 "APP_VERSION_BRIDGE_RETURN\tverified_package_version_candidate", "PHASE\towner_getter_return",
                 "OWNER_GETTER_RESULT\t1\t1\t1", "OWNER_VTABLE_MATCH\t1"):
        if lines.count(line) != 1:
            raise ValueError("Linux bridge output incomplete")
    return {"status": "RUNTIME_OBSERVED_LIMITED", "android_version": "15.535.13",
            "linux_version_bytes_checked": True, "linux_owner_constructor_returned": True,
            "catalog_ready_pass": False, "configuration_contract": "UNRESOLVED", "backup_equivalence": "UNRESOLVED",
            "limitations": ["one fixed package and successful version lookup only",
                            "exception semantics and complete object lifetime not validated",
                            "not a natural Linux cold start or battle lifecycle"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("android", "before_dir", "after_dir", "output"):
        parser.add_argument("--" + name.replace("_", "-"), type=Path, required=True)
    args = parser.parse_args()
    android = json.loads(args.android.read_text())
    results = [json.loads((directory / "result.json").read_text()) for directory in (args.before_dir, args.after_dir)]
    for result, directory in zip(results, (args.before_dir, args.after_dir)):
        for name in ("observation.json", "stdout.log"):
            if result.get("outputs", {}).get(name) != sha(directory / name):
                raise ValueError("Linux output identity mismatch")
    observations = [json.loads((directory / "observation.json").read_text()) for directory in (args.before_dir, args.after_dir)]
    summary = compare(android, results[0], observations[0], results[1], observations[1],
                      (args.after_dir / "stdout.log").read_text())
    summary.update(android_sha256=sha(args.android), before_sha256=sha(args.before_dir / "result.json"),
                   after_sha256=sha(args.after_dir / "result.json"))
    with args.output.open("x") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
