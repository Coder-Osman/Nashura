"""Run the pinned full replay in a Linux guest using the direct ART entry.

Default: validate inputs and print a plan. --apply creates a new evidence
directory. Settings contain paths and SHA256 identities, never credentials.
The extracted ART closure is operator supplied; its transitive libraries are
not certified minimal by this runner. No Android service is started.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


ENGINE_SHA256 = "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783"
APK_SHA256 = "a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f"
DEX_SHA256 = "c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6"
RUNTIME_SHA256 = {
    "apex/com.android.art/lib64/libart.so":
        "35a64b7c823728e10e5dee31689977417d5556a5358e4fecc568e1b133baed66",
    "system/lib64/libandroid_runtime.so":
        "806a7753d28556488dc7c6535b111128e6ad60b3d25db2a8183a4643f9c7add6",
    "system/lib64/libutils.so":
        "f67a4a20450acb622d4e8dfc56a856f17ce18c6c45059274598862163f5e8ef1",
}
BOOT = ["/apex/com.android.art/javalib/" + name + ".jar" for name in
        ("core-oj", "core-libart", "core-icu4j", "okhttp", "bouncycastle", "apache-xml")]
BOOT += ["/apex/com.android.conscrypt/javalib/conscrypt.jar",
         "/data/local/tmp/framework-sdk.jar",
         "/apex/com.android.os.statsd/javalib/framework-statsd.jar"]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("absolute canonical path required: " + str(path))
    return path


def validate(settings, output):
    names = ("closure", "namespace", "linker_config", "runtime", "probe_dex",
             "observer", "apk", "replay")
    paths = {name: canonical(settings[name]) for name in names}
    closure = paths["closure"]
    if closure == Path("/") or not closure.is_dir():
        raise ValueError("extracted closure directory required")
    output = canonical(output)
    if output.exists() or closure in output.parents or output in closure.parents:
        raise ValueError("new output directory outside closure required")
    expected_parent = closure / "apex/com.android.art/bin"
    if paths["runtime"].parent != expected_parent:
        raise ValueError("direct runtime must use the existing ART linker section")
    required = [paths[name] for name in names if name != "closure"]
    required += [closure / name.lstrip("/") for name in BOOT]
    required += [closure / name for name in (
        "data/local/tmp/current-game-classes.dex", "data/local/tmp/libg-input/libg.so",
        "data/local/tmp/libart_engine_jni_probe.so",
        "apex/com.android.runtime/bin/linker64",
        "apex/com.android.runtime/lib64/bionic/libc.so",
        "apex/com.android.art/lib64/libart.so",
        "system/lib64/libandroid_runtime.so", "system/lib64/libutils.so",
        "dev/__properties__/property_info", "dev/__properties__/properties_serial")]
    identities = settings["identities"]
    for path in required:
        if str(path) not in identities:
            raise ValueError("missing required input identity: " + str(path))
    for name, expected in identities.items():
        path = canonical(name)
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid input SHA256: " + name)
        if not path.is_file() or sha256(path) != expected:
            raise ValueError("input identity mismatch: " + name)
    fixed = {paths["apk"]: APK_SHA256,
             closure / "data/local/tmp/libg-input/libg.so": ENGINE_SHA256,
             closure / "data/local/tmp/current-game-classes.dex": DEX_SHA256}
    fixed.update({closure / name: expected for name, expected in RUNTIME_SHA256.items()})
    if any(identities[str(path)] != expected for path, expected in fixed.items()):
        raise ValueError("wrong pinned game build")
    if not os.access(paths["runtime"], os.X_OK):
        raise ValueError("executable direct runtime required")
    if not 2 <= paths["replay"].stat().st_size < 65536:
        raise ValueError("replay size outside supported range")
    # Pin the existing local property contexts too, including their values.
    for path in (closure / "dev/__properties__").iterdir():
        if path.is_file() and str(path) not in identities:
            raise ValueError("missing property input identity: " + str(path))
    return paths, output


def build_plan(settings, output):
    paths, output = validate(settings, output)
    closure = paths["closure"]
    inside = lambda path: "/" + str(path.relative_to(closure))
    outside = lambda path: "/oldroot" + str(path)
    command = ["timeout", "-k", "3s", "45s", "unshare", "-Ur", "-m", "-n", "-i",
               "/bin/sh", str(paths["namespace"]), str(closure), str(paths["linker_config"]),
               inside(paths["runtime"]), "-Xbootclasspath:" + ":".join(BOOT),
               "-Djava.library.path=/data/local/tmp/libg-input", "--probe-args",
               "/data/local/tmp/libg-input/libg.so", "/data/local/tmp/libart_engine_jni_probe.so",
               outside(paths["apk"]), outside(output / "probe-data"), "callbacks", "apk-context",
               outside(paths["observer"]), "native-create", "native-destroy", "native-active",
               "game-state-action-step", outside(paths["replay"]),
               settings["identities"][str(paths["replay"])]]
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C",
                   "CLASSPATH": outside(paths["probe_dex"]) + ":/data/local/tmp/current-game-classes.dex",
                   "NASHURA_BATTLE_STEPS": "10000", "NASHURA_SNAPSHOT_ADAPTER": "1",
                   "NASHURA_QUIET": "1", "NASHURA_QUIET_SNAPSHOTS": "1",
                   "NASHURA_BENCHMARK_VALIDATE": "1",
                   "NASHURA_TRACE_SNAPSHOTS": outside(output / "snapshots.bin")}
    return {"schema": "nashura.headless-local-run.v1", "command": command,
            "environment": environment, "identities": settings["identities"],
            "output": str(output), "snapshot_scope": "explicit original-serializer adapter",
            "whole_goal_complete": False}


def verify_completion(exit_code, stdout):
    if exit_code != 0:
        raise ValueError("runtime exit code: " + str(exit_code))
    required = ("HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1",
                "HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1", "HEADLESS_REJECTED_PM_CALLS=0")
    lines = stdout.splitlines()
    if any(marker not in lines for marker in required):
        raise ValueError("missing direct-entry completion evidence")
    matches = re.findall(r"^HEADLESS_GAME_STATE_STEP_RETURN .*tick_after=(\d+) .*finalized=1$",
                         stdout, re.MULTILINE)
    if len(matches) != 1 or int(matches[0]) <= 0:
        raise ValueError("missing unique native finalization evidence")
    return int(matches[0])


def run(settings, output, apply=False):
    plan = build_plan(settings, output)
    if not apply:
        return plan
    from compare_headless_snapshots import inspect_archive
    output = Path(plan["output"])
    output.mkdir()
    with (output / "plan.json").open("x") as stream:
        json.dump(plan, stream, indent=2)
        stream.write("\n")
    with (output / "stdout.log").open("xb") as stdout, (output / "stderr.log").open("xb") as stderr:
        result = subprocess.run(plan["command"], env=plan["environment"], stdout=stdout, stderr=stderr)
    (output / "exit.txt").write_text(str(result.returncode) + "\n")
    final_tick = verify_completion(result.returncode, (output / "stdout.log").read_text())
    archive = inspect_archive(output / "snapshots.bin", final_tick)
    summary = {"status": "NATIVE_FINAL_AND_ARCHIVE_OBSERVED_LIMITED",
               "final_tick": final_tick, "records": len(archive["frames"]),
               "archive_bytes": archive["bytes"], "archive_sha256": archive["sha256"],
               "whole_goal_complete": False}
    with (output / "result.json").open("x") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("settings", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(json.loads(args.settings.read_text()), args.output, args.apply), indent=2))


if __name__ == "__main__":
    main()
