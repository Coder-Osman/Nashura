import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from plugin_protocol import sha, summarize
from verify_trace import address_identity, limited, validate_trajectory


def read(path):
    return json.loads(path.read_text())


def comparable(report):
    return {name: report[name] for name in ("decisions", "outcomes", "executed", "steps", "terminal",
        "policy_closed_tick", "final_clock", "match_seed", "execution_seed", "source_sha256", "engine_sha256")}


def compare_reports(reports, measurements):
    if len(reports) != 3 or len(measurements) != 3:
        raise ValueError("runtime comparison requires all three frozen variants")
    reference = comparable(reports[0])
    for report in reports[1:]:
        actual = comparable(report)
        if actual != reference:
            names = [name for name in reference if reference[name] != actual[name]]
            raise ValueError("runtime trajectory differs in: " + ", ".join(names))
    projected = []
    for report, value in zip(reports, measurements, strict=True):
        limited(value)
        if value["schema"] != "FairFinalRuntimeMeasurements/v1":
            raise ValueError("runtime measurement schema differs")
        records = value["records"]
        final = report["final"]["tick"]
        ticks = sorted({*range(300, final + 1, 300), final})
        if [row["tick"] for row in records] != ticks:
            raise ValueError("runtime periodic observation coverage differs")
        for row in records:
            if len(bytes.fromhex(row["observation_sha256"])) != 32:
                raise ValueError("runtime semantic digest identity malformed")
            digest = row["process_local_digest"]
            if digest["tick"] != row["tick"] or digest["scope"] != "native-payload+global-mt2504.v1":
                raise ValueError("runtime native digest scope differs")
            for name in ("sha256", "nativeSha256"):
                if len(bytes.fromhex(digest[name])) != 32:
                    raise ValueError("runtime native digest identity malformed")
        projected.append([{name: row[name] for name in ("tick", "observation_sha256")} for row in records])
    if any(value != projected[0] for value in projected[1:]):
        raise ValueError("runtime periodic semantic observations differ")
    return dict(compared_variants=len(reports), periodic_boundaries=len(projected[0]),
        decisions=sum(len(group["choices"]) for group in reference["decisions"]),
        native_digests_recorded_per_process=True, cross_process_native_payload_equality_claimed=False)


def validate_build_archive(attempt, launch_sha):
    if sha(attempt / "launch.json") != launch_sha:
        raise ValueError("plugin build external identity differs")
    launch, receipt = read(attempt / "launch.json"), read(attempt / "download.json")
    limited(launch)
    limited(receipt)
    if receipt.get("launch_sha256") != launch_sha or receipt.get("worker_stopped") is not True:
        raise ValueError("plugin build receipt ownership differs")
    files, archive = receipt["files"], attempt / "archive"
    required = {"build_runtime.py", "completion.json", "build-evidence.json", "configure.log", "build.log", "worker.log", "qemu-help.txt"}
    actual = {path.relative_to(archive).as_posix() for path in archive.rglob("*") if path.is_file()}
    if not required <= set(files) or "failure.json" in files or actual != set(files):
        raise ValueError("plugin build execution evidence incomplete")
    for name, metadata in files.items():
        path = archive / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink()
                or path.resolve() != archive.resolve() / name or sha(path) != metadata["sha256"]
                or path.stat().st_size != metadata["bytes"]):
            raise ValueError("plugin build archived bytes differ")
    result = read(archive / "completion.json")
    limited(result)
    from build_runtime import SOURCE_SHA256, SOURCE_URL, PACKAGES
    if (result["builder_sha256"] != launch["builder_sha256"]
            or sha(archive / "build_runtime.py") != launch["builder_sha256"]
            or result["source_sha256"] != SOURCE_SHA256 or result["source_url"] != SOURCE_URL
            or result["threads"] != 4 or {name: item["sha256"] for name, item in result["packages"].items()} != PACKAGES
            or result["steps"]["configure"]["log_sha256"] != sha(archive / "configure.log")
            or result["steps"]["build"]["log_sha256"] != sha(archive / "build.log")
            or "-plugin" not in (archive / "qemu-help.txt").read_text()):
        raise ValueError("plugin build pinned source or dependency provenance differs")
    return result


def verify(attempt, launch_sha, receipt_sha, *, ancestor, build_attempt, snapshot_parent):
    if sha(attempt / "launch.json") != launch_sha or sha(attempt / "download.json") != receipt_sha:
        raise ValueError("plugin external pinned identities differ")
    launch, receipt = read(attempt / "launch.json"), read(attempt / "download.json")
    limited(launch)
    limited(receipt)
    if (launch["schema"] != "FairFinalPluginLaunch/v1" or receipt["schema"] != "FairFinalPluginDownload/v1"
            or receipt["launch_sha256"] != launch_sha or receipt["worker_stopped"] is not True):
        raise ValueError("plugin archive contract differs")
    archive, files = attempt / "archive", receipt["files"]
    required = {*launch["source_sha256"], "settings.json", "worker.log", "compile.json", "compile.log", "completion.json"}
    allowed = set(required)
    for variant in ("baseline", "upgraded", "plugin"):
        names = ("plugin-settings.json", "report.json", "measurements.json", "cleanup.json", "engine-owner.json",
            "probe-process.json", "engine/process.json", "address-evidence.json", "phase-completion.json")
        required.update(variant + "/" + name for name in names)
        allowed.update(variant + "/" + name for name in (*names, "engine.log", "probe.log", "progress.json"))
    required.update(("plugin/qemu.log", "plugin/trace-summary.json"))
    if "plugin_seal.py" in launch["source_sha256"]:
        required.add("plugin/trace-seal.json")
    if launch["settings"].get("arm_after_ready"):
        required.add("plugin/arm-base.txt")
    allowed.update(required)
    actual = {path.relative_to(archive).as_posix() for path in archive.rglob("*") if path.is_file()}
    if not required <= set(files) <= allowed or actual != set(files):
        raise ValueError("plugin missing evidence or archived failure")
    for name, metadata in files.items():
        path = archive / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink()
                or path.resolve() != archive.resolve() / name or sha(path) != metadata["sha256"]
                or path.stat().st_size != metadata["bytes"] or not 0 <= metadata["bytes"] <= 256 * 1024**2):
            raise ValueError("plugin archived member bytes differ")
    for name, expected in launch["source_sha256"].items():
        if files[name]["sha256"] != expected:
            raise ValueError("plugin uploaded source identity differs")
    from run_plugin import runtime_sources
    if set(launch["source_sha256"]) != set(runtime_sources()):
        raise ValueError("plugin pinned source member set differs")
    if (sha(ancestor / "launch.json") != launch["ancestor_launch_sha256"]
            or sha(build_attempt / "launch.json") != launch["build_launch_sha256"]):
        raise ValueError("plugin parent source and builder identities differ")
    parent = read(ancestor / "launch.json")
    from run_trace import validate_parent
    proof = validate_parent(snapshot_parent, parent["parent_launch_sha256"], parent["parent_receipt_sha256"])
    if proof != parent["parent_verification"] or proof["engine_sha256"] != launch["settings"]["engine_sha256"]:
        raise ValueError("plugin parent snapshot evidence differs")
    build = validate_build_archive(build_attempt, launch["build_launch_sha256"])
    if (launch["frozen_runtime_sha256"] != parent["frozen_runtime_sha256"]
            or read(build_attempt / "launch.json") != launch["builder"]
            or build != launch["build"]
            or sha(build_attempt / "archive/build_runtime.py") != launch["builder"]["builder_sha256"]):
        raise ValueError("plugin build execution and frozen runtime provenance differs")
    settings = read(archive / "settings.json")
    completion = read(archive / "completion.json")
    if (settings != launch["settings"] or sha(archive / "settings.json") != launch["settings_sha256"]
            or completion["settings_sha256"] != launch["settings_sha256"]
            or completion["status"] != "completed_requires_independent_verification"
            or settings["source_sha256"] != launch["source_sha256"]):
        raise ValueError("plugin settings or completion differs")
    if settings["root"] != launch.get("remote", settings["root"]) or settings["port"] not in (29787, 29789):
        raise ValueError("plugin registered root or port differs")
    for name in ("project", "base_project", "puct_root", "probe", "engine_sha256", "qemu", "qemu_sha256", "service_sha256"):
        if settings[name] != parent["settings"][name]:
            raise ValueError("plugin retained parent runtime setting differs: " + name)
    limited(completion)
    if read(archive / "compile.json") != launch["compile"]:
        raise ValueError("plugin compile evidence differs")
    if (launch["compile"]["source_sha256"] != launch["source_sha256"]["random_entries.c"]
            or launch["compile"]["plugin_sha256"] != settings["plugin_sha256"]
            or launch["compile"]["header_sha256"] != launch["build"]["plugin_header_sha256"]
            or launch["build"]["qemu_sha256"] != settings["plugin_qemu_sha256"]):
        raise ValueError("plugin binary and header provenance differs")
    reports, measurements, processes, phases, trajectories = [], [], [], [], []
    center = None
    for variant in ("baseline", "upgraded", "plugin"):
        folder = archive / variant
        report, process, cleanup = (read(folder / name) for name in ("report.json", "engine/process.json", "cleanup.json"))
        owner, probe = (read(folder / name) for name in ("engine-owner.json", "probe-process.json"))
        configuration = read(folder / "plugin-settings.json")
        if configuration != dict(settings, variant=variant,
                center_base=center if variant == "plugin" and not settings.get("arm_after_ready") else None):
            raise ValueError("plugin variant configuration differs")
        if (cleanup["clean"] is not True or cleanup["port_released"] is not True or cleanup["exit_code"] != 0
                or cleanup["process"] != process or cleanup["launcher"] != owner["process"]
                or process["phase"] != "stopped" or process["ready"] is not False
                or owner["process"] != {name: process[name] for name in ("pid", "start_ticks")}
                or probe["exit_code"] != 0 or not 0 <= probe["seconds"] <= 965):
            raise ValueError("plugin native owned process cleanup differs")
        expected_qemu = settings["qemu"] if variant == "baseline" else settings["plugin_qemu"]
        expected_sha = settings["qemu_sha256"] if variant == "baseline" else settings["plugin_qemu_sha256"]
        if process["random_plugin_trace"] != dict(variant=variant,
                settings_sha256=sha(folder / "plugin-settings.json"),
                launcher_sha256=launch["source_sha256"]["plugin_launcher.py"], qemu_sha256=expected_sha,
                plugin_sha256=settings["plugin_sha256"] if variant == "plugin" else None):
            raise ValueError("plugin native runtime attestation differs")
        if process["phase_snapshot_engine"] != dict(path=settings["probe"],
                sha256=settings["engine_sha256"], launcher_sha256=launch["source_sha256"]["plugin_launcher.py"],
                service_sha256=settings["service_sha256"]):
            raise ValueError("plugin engine attestation differs")
        arguments = process["argv"]
        if (arguments[0] != expected_qemu or arguments.count(settings["probe"]) != 1
                or arguments[-1] != str(settings["port"]) or process["port"] != settings["port"]
                or any(flag in arguments for flag in ("-d", "-D", "-dfilter", "-singlestep", "-one-insn-per-tb"))):
            raise ValueError("plugin native runtime arguments differ")
        if variant != "baseline" and settings.get("reserved_va") is not None:
            if (settings["reserved_va"] != 1024**4 or arguments.count("-R") != 1
                    or arguments[arguments.index("-R") + 1] != hex(settings["reserved_va"])):
                raise ValueError("plugin reserved guest address space differs")
        elif "-R" in arguments:
            raise ValueError("plugin unexpectedly reserved guest address space")
        if variant == "plugin":
            from plugin_protocol import filter_spec
            if settings.get("arm_after_ready"):
                center = report["before"]["libg_base"]
                if (folder / "arm-base.txt").read_bytes() != f"{center:016x}\n".encode():
                    raise ValueError("plugin actual process arming record differs")
                specification = (f"{settings['plugin']},arm={settings['root']}/plugin/arm-base.txt,"
                    f"output={settings['root']}/plugin/qemu.log")
            else:
                window = filter_spec(center)
                specification = (f"{settings['plugin']},first={window['first_base']:x},"
                    f"last={window['last_base']:x},output={settings['root']}/plugin/qemu.log")
            if arguments.count("-plugin") != 1 or arguments[arguments.index("-plugin") + 1] != specification:
                raise ValueError("plugin instrumentation arguments differ")
        elif "-plugin" in arguments:
            raise ValueError("runtime comparison unexpectedly instrumented")
        limited(report)
        if (report["source_sha256"] != launch["frozen_runtime_sha256"]
                or report["engine_sha256"] != settings["engine_sha256"] or report["mode"] != "trace"
                or report["status"] != "completed_pending_trace_verification"
                or report.get("match_seed") != 78102011 or report.get("execution_seed") != 78102021
                or not 0 <= report["seconds"] <= 905):
            raise ValueError("plugin probe runtime identity differs")
        if read(folder / "address-evidence.json") != report["before"]:
            raise ValueError("plugin before-reset address evidence differs")
        for name in ("before", "opening", "final"):
            address_identity(report[name], process, cold=name == "before")
        if (len({report[name]["libg_base"] for name in ("before", "opening", "final")}) != 1
                or report["before"]["tick"] != -1 or report["before"]["manager"] != 0
                or report["opening"]["generation"] != report["final"]["generation"]
                or report["opening"]["manager"] != report["final"]["manager"]):
            raise ValueError("plugin runtime lifetime differs")
        trajectories.append(validate_trajectory(report))
        reports.append(report)
        measurements.append(read(folder / "measurements.json"))
        processes.append((process["child_pid"], process["child_start_ticks"]))
        phases.append(read(folder / "phase-completion.json"))
        limited(phases[-1])
        if phases[-1]["variant"] != variant or not 0 < phases[-1]["seconds"] <= 1200:
            raise ValueError("plugin phase completion timing differs")
        if variant == "upgraded":
            center = report["opening"]["libg_base"]
    if len(set(processes)) != 3:
        raise ValueError("plugin comparison reused a native process")
    comparison = compare_reports(reports, measurements)
    report = reports[-1]
    opening = report["opening"]
    if settings.get("arm_after_ready"):
        with (archive / "plugin/qemu.log").open("rb") as stream:
            if stream.readline() != b"FairFinalPluginEntries/v3\n":
                raise ValueError("plugin actual process arm requires explicit v3 trace")
    seal = None
    if "plugin_seal.py" in launch["source_sha256"]:
        from plugin_seal import seal_identity
        seal = read(archive / "plugin/trace-seal.json")
        if seal != seal_identity(archive / "plugin", report, read(archive / "plugin/cleanup.json")):
            raise ValueError("plugin supervised seal execution evidence differs")
    summary = summarize(archive / "plugin/qemu.log", base=opening["libg_base"], center=center,
        reset_offset=report["reset_offset"], battle_offset=report["battle_offset"],
        final_offset=report["final_offset"], global_object=opening["libg_base"] + 0x19FC368,
        manager_object=opening["manager"] + 0xA0, seal=seal)
    if summary != read(archive / "plugin/trace-summary.json"):
        raise ValueError("plugin summary differs from raw callbacks")
    counts = Counter((row["phase"], row["function"], row["object_kind"], row["caller_offset"])
        for row in summary["records"])
    return dict(schema="FairFinalPluginVerification/v1", status="verified_limited_scope",
        launch_sha256=launch_sha, receipt_sha256=receipt_sha, verifier_sha256=sha(__file__),
        comparison=comparison, trajectories=trajectories, phase_seconds={
            variant: phase["seconds"] for variant, phase in zip(("baseline", "upgraded", "plugin"), phases, strict=True)},
        phase_counts=summary["phase_counts"], captured_entries=summary["captured_entries"],
        trace_closure="supervised_after_native_terminal" if seal is not None else "plugin_atexit",
        sampled_pc_differs=sum(row.get("sampled_pc", row["pc"]) != row["pc"] for row in summary["records"]),
        caller_groups=[dict(phase=key[0], function=key[1], object_kind=key[2], caller_offset=hex(key[3]), entries=count)
            for key, count in sorted(counts.items())], trace_bytes=files["plugin/qemu.log"]["bytes"],
        other_algorithms_covered=False, hidden_random_coverage_closed=False,
        training_admissible=False, gates_closed=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ancestor", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--snapshot-parent", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.attempt, args.launch_sha, args.receipt_sha,
        ancestor=args.ancestor, build_attempt=args.build, snapshot_parent=args.snapshot_parent)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
