import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

from trace_protocol import LOG_FLAGS, check_base, filter_spec, sha, summarize


ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / name) for name in
    ("fair-final-combat-snapshot-v1", "fair-final-history-v2/project")]
from combat_protocol import validate_selection


def read(path):
    return json.loads(path.read_text())


def limited(record):
    if any(record.get(name) is not False for name in ("training_admissible", "gates_closed")):
        raise ValueError("execution trace scope exceeds diagnostic evidence")


def audit_execution_attempts(path):
    header = re.compile(rb"^Trace \d+: \S+ \[[0-9a-fA-F]+/([0-9a-fA-F]+)/")
    pc_line = re.compile(rb"^\s*PC=([0-9a-fA-F]{16})\b")
    pending, cpu_seen, blocks = None, False, 0
    with path.open("rb") as stream:
        for line in stream:
            if b"Stopped execution of TB chain before" in line:
                raise ValueError("QEMU reported an unexecuted TB attempt")
            match = header.match(line)
            if match:
                if pending is not None:
                    raise ValueError("interleaved or incomplete QEMU execution header")
                pending, cpu_seen = int(match[1], 16), False
            match = pc_line.match(line)
            if match:
                if pending != int(match[1], 16) or cpu_seen:
                    raise ValueError("QEMU CPU state differs from execution header")
                cpu_seen = True
            if b"PSTATE=" in line:
                if pending is None or not cpu_seen:
                    raise ValueError("QEMU execution header lacks matching CPU state")
                pending, cpu_seen, blocks = None, False, blocks + 1
    if pending is not None or blocks == 0:
        raise ValueError("QEMU execution attempts are empty or incomplete")
    return blocks


def address_identity(record, process, *, cold=False):
    if (record.get("readonly") is not True or record.get("child_pid") != process["child_pid"]
            or record.get("child_start_ticks") != process["child_start_ticks"]
            or type(record.get("libg_base")) is not int or record["libg_base"] <= 0
            or type(record.get("manager")) is not int or record["manager"] < (0 if cold else 1)
            or record["libg_base"] % 4096
            or type(record.get("tick")) is not int or record["tick"] < (-1 if cold else 0)
            or any(type(record.get(name)) is not int or record[name] < 0
                for name in ("generation", "stateEpoch"))):
        raise ValueError("execution trace readonly native identity differs")


def validate_trajectory(report):
    terminal = report["terminal"]
    if (not isinstance(terminal, dict) or terminal.get("finalized") is not True
            or type(terminal.get("worldResult")) is not int
            or terminal["worldResult"] not in (-1, 0, 1)
            or terminal.get("worldResultRaw") != terminal["worldResult"]
            or terminal.get("winner") != (None if terminal["worldResult"] == -1 else terminal["worldResult"])):
        raise ValueError("execution trace lacks complete native finalization")
    if terminal["worldResult"] == -1 and (not isinstance(terminal.get("crownsRaw"), list)
            or len(terminal["crownsRaw"]) != 2 or terminal["crownsRaw"][0] != terminal["crownsRaw"][1]):
        raise ValueError("execution trace draw crowns differ")
    opening, final = report["opening"]["tick"], report["final"]["tick"]
    if (opening != 90 or type(report["steps"]) is not int or not 0 < report["steps"] <= 7200 - opening
            or final != opening + report["steps"] or terminal.get("tick") != final):
        raise ValueError("execution trace complete tick coverage differs")
    choices, outcomes = {}, {}
    for group in report["decisions"]:
        tick = group["tick"]
        if type(tick) is not int or tick in choices or not opening <= tick < final:
            raise ValueError("execution trace duplicate or out-of-range decisions")
        choices[tick] = group["choices"]
        if not choices[tick] or not set(choices[tick]) <= {"0", "1"}:
            raise ValueError("execution trace owner coverage differs")
        for row in choices[tick].values():
            validate_selection(row)
            if not isinstance(row["fair_sha"], str) or len(bytes.fromhex(row["fair_sha"])) != 32:
                raise ValueError("execution trace FAIR identity differs")
    if list(choices) != sorted(choices):
        raise ValueError("execution trace decision order differs")
    for group in report["outcomes"]:
        tick = group["tick"]
        if type(tick) is not int or tick in outcomes or not opening < tick <= final or not group["outcomes"]:
            raise ValueError("execution trace outcome coverage differs")
        outcomes[tick] = group["outcomes"]
    if list(outcomes) != sorted(outcomes):
        raise ValueError("execution trace outcome order differs")
    closed = report["policy_closed_tick"]
    if type(closed) is not int or not opening < closed <= final:
        raise ValueError("execution trace policy closure differs")
    pending, identity, ready, evidence = {}, 0, {0, 1}, []
    executed = Counter()
    for tick in range(opening, final + 1):
        due = sorted((plan for plan in pending.values() if plan["due"] == tick), key=lambda plan: plan["owner"])
        expected = {str(plan["identity"]): "executed" for plan in due if plan["action"]["card_id"] is not None}
        if outcomes.get(tick, {}) != expected:
            raise ValueError("execution trace missing or rejected due action")
        if tick > opening:
            ready = {0, 1} if expected else set()
            for plan in due:
                ready.add(plan["owner"])
                evidence.append(dict(reservation=plan["identity"], owner=plan["owner"], tick=tick,
                    reason="executed" if plan["action"]["card_id"] is not None else "wait_expired"))
                if plan["action"]["card_id"] is not None:
                    executed[plan["owner"]] += 1
                del pending[plan["owner"]]
            for owner in (list(pending) if tick >= closed else sorted(ready)):
                plan = pending.pop(owner, None)
                if plan is not None:
                    evidence.append(dict(reservation=plan["identity"], owner=owner, tick=tick,
                        reason="terminal_expired" if tick >= closed else "preempted"))
            if tick >= closed:
                ready = set()
        if tick == final:
            break
        actual = choices.get(tick, {})
        if set(actual) != {str(owner) for owner in ready}:
            raise ValueError("execution trace simultaneous decision boundary differs")
        for owner in sorted(ready):
            action = actual[str(owner)]["action"]
            duration = 60 if action["card_id"] is None else action["delay_slot"] * 2
            pending[owner] = dict(identity=identity, owner=owner, action=action, due=tick + duration)
            identity += 1
    if not all(executed[owner] > 0 for owner in (0, 1)) or report["executed"] != sum(executed.values()):
        raise ValueError("execution trace both-player ACT confirmation missing")
    if report["final_clock"] != dict(schema="fair-final-decision-clock.v1", tick=final,
            next_identity=identity, pending=[], ready=[], evidence=evidence):
        raise ValueError("execution trace complete reservation settlement differs")
    return dict(decision_groups=len(choices), decisions=identity, executed_by_owner=dict(executed),
        final_tick=final, winner=terminal["winner"])


def validate_native(folder, mode, settings, frozen_runtime, center=None):
    report, process = read(folder / "report.json"), read(folder / "engine/process.json")
    if read(folder / "address-evidence.json") != report["before"]:
        raise ValueError("execution trace registered base identity differs")
    cleanup, owner, probe = (read(folder / name) for name in
        ("cleanup.json", "engine-owner.json", "probe-process.json"))
    configuration = read(folder / "trace-settings.json")
    if configuration != dict(mode=mode, center_base=center, qemu_sha256=settings["qemu_sha256"],
            service_sha256=settings["service_sha256"]):
        raise ValueError("execution trace frozen logging configuration differs")
    if (cleanup["clean"] is not True or cleanup["port_released"] is not True
            or cleanup["exit_code"] != 0 or cleanup["process"] != process
            or cleanup["launcher"] != owner["process"] or process["phase"] != "stopped"
            or process["ready"] is not False or process["port"] != settings["port"]
            or owner["process"] != {name: process[name] for name in ("pid", "start_ticks")}
            or probe["exit_code"] != 0 or not 0 <= probe["seconds"] <= 965):
        raise ValueError("execution trace owned process cleanup differs")
    expected = dict(settings_sha256=sha(folder / "trace-settings.json"),
        launcher_sha256=settings["source_sha256"]["trace_launcher.py"],
        protocol_sha256=settings["source_sha256"]["trace_protocol.py"],
        startup_deadline_seconds=600, startup_service_sha256=settings["startup_service_sha256"])
    if (process["random_execution_trace"] != expected
            or process["phase_snapshot_engine"] != dict(path=settings["probe"],
                sha256=settings["engine_sha256"], launcher_sha256=expected["launcher_sha256"],
                service_sha256=settings["service_sha256"])):
        raise ValueError("execution trace native attestation differs")
    arguments = process["argv"]
    if (arguments[0] != settings["qemu"] or arguments.count(settings["probe"]) != 1
            or arguments[-1] != str(settings["port"])
            or any(flag in arguments for flag in ("-one-insn-per-tb", "-singlestep"))):
        raise ValueError("execution trace QEMU identity or semantics differ")
    if mode == "trace":
        for flag, value in (("-d", LOG_FLAGS), ("-dfilter", filter_spec(center)["expression"]),
                ("-D", settings["root"] + "/trace/qemu.log")):
            if arguments.count(flag) != 1 or arguments[arguments.index(flag) + 1] != value:
                raise ValueError("execution trace complete logging flags differ")
    elif any(flag in arguments for flag in ("-d", "-dfilter", "-D")):
        raise ValueError("execution trace calibration unexpectedly logged")
    limited(report)
    if (report["schema"] != "FairFinalRandomExecutionProbe/v1" or report["mode"] != mode
            or report["status"] != ("calibrated" if mode == "calibration" else "completed_pending_trace_verification")
            or report["engine_sha256"] != settings["engine_sha256"]
            or report["source_sha256"] != frozen_runtime
            or report["match_seed"] != 78102011 or report["execution_seed"] != 78102021
            or not 0 <= report["seconds"] <= 905):
        raise ValueError("execution trace probe contract differs")
    for name in ("before", "opening", "final"):
        address_identity(report[name], process, cold=name == "before")
    if (len({report[name]["libg_base"] for name in ("before", "opening", "final")}) != 1
            or report["opening"]["tick"] != 90
            or report["before"]["tick"] != -1 or report["before"]["manager"] != 0
            or report["opening"]["generation"] != report["final"]["generation"]
            or report["opening"]["manager"] != report["final"]["manager"]):
        raise ValueError("execution trace native lifetime differs")
    if mode == "calibration":
        if (report["terminal"] is not None or report["decisions"] or report["outcomes"]
                or report["steps"] != 0 or report["final"]["tick"] != 90):
            raise ValueError("execution trace calibration trajectory differs")
        return report, process, None
    check_base(center, report["opening"]["libg_base"])
    trajectory = validate_trajectory(report)
    audited_blocks = audit_execution_attempts(folder / "qemu.log")
    opening = report["opening"]
    summary = summarize(folder / "qemu.log", base=opening["libg_base"],
        reset_offset=report["reset_offset"], battle_offset=report["battle_offset"],
        final_offset=report["final_offset"], global_object=opening["libg_base"] + 0x19FC368,
        manager_object=opening["manager"] + 0xA0)
    if summary != read(folder / "trace-summary.json"):
        raise ValueError("execution trace summary differs from raw register log")
    aggregated = Counter((row["phase"], row["function"], row["object_kind"], row["caller_offset"])
        for row in summary["records"])
    result = dict(trajectory=trajectory, audited_execution_blocks=audited_blocks, phase_counts=summary["phase_counts"],
        captured_entries=summary["captured_entries"], trace_sha256=summary["trace_sha256"],
        caller_groups=[dict(phase=key[0], function=key[1], object_kind=key[2],
            caller_offset=hex(key[3]), entries=count) for key, count in sorted(aggregated.items())])
    return report, process, result


def verify(attempt, launch_sha, receipt_sha, *, parent):
    from run_trace import PORT, upload_sources, validate_parent

    if sha(attempt / "launch.json") != launch_sha or sha(attempt / "download.json") != receipt_sha:
        raise ValueError("execution trace external pinned identities differ")
    launch, receipt = read(attempt / "launch.json"), read(attempt / "download.json")
    limited(launch); limited(receipt)
    if (launch["schema"] != "FairFinalRandomExecutionLaunch/v1"
            or receipt["schema"] != "FairFinalRandomExecutionDownload/v1"
            or receipt["launch_sha256"] != launch_sha or receipt["worker_stopped"] is not True):
        raise ValueError("execution trace archive contract differs")
    proof = validate_parent(parent, launch["parent_launch_sha256"], launch["parent_receipt_sha256"])
    if proof != launch["parent_verification"]:
        raise ValueError("execution trace parent evidence differs")
    if (sha(ROOT / "run_trace.py") != launch["runner_sha256"]
            or sha(ROOT / "CONTRACT.md") != launch["contract_sha256"]):
        raise ValueError("execution trace local implementation differs")
    sources = {name: hashlib.sha256(content).hexdigest() for name, content in upload_sources().items()}
    archive, files = attempt / "archive", receipt["files"]
    required = {*sources, "settings.json", "worker.log", "completion.json"}
    allowed = set(required)
    for mode in ("calibration", "trace"):
        required.update(mode + "/" + name for name in ("trace-settings.json", "report.json",
            "cleanup.json", "engine-owner.json", "probe-process.json", "engine/process.json", "address-evidence.json"))
        allowed.update(mode + "/" + name for name in ("trace-settings.json", "report.json", "cleanup.json",
            "engine-owner.json", "probe-process.json", "engine/process.json", "engine.log", "probe.log", "status.json",
            "address-evidence.json", "progress.json"))
    required.update(("trace/qemu.log", "trace/trace-summary.json")); allowed.update(required)
    actual = {path.relative_to(archive).as_posix() for path in archive.rglob("*") if path.is_file()}
    if not required <= set(files) <= allowed or actual != set(files):
        raise ValueError("execution trace missing evidence or archived failure")
    for name, metadata in files.items():
        path = archive / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink()
                or path.resolve() != archive.resolve() / name
                or sha(path) != metadata["sha256"] or path.stat().st_size != metadata["bytes"]
                or not 0 <= metadata["bytes"] <= 256 * 1024**2):
            raise ValueError("execution trace archived file bytes differ")
    uploads = {**sources, "settings.json": files["settings.json"]["sha256"]}
    if (launch["uploaded_sha256"] != uploads
            or any(files[name]["sha256"] != digest for name, digest in uploads.items())):
        raise ValueError("execution trace uploaded source identity differs")
    settings, completion = read(archive / "settings.json"), read(archive / "completion.json")
    if (settings != launch["settings"] or settings["source_sha256"] != sources or settings["port"] != PORT
            or settings["root"] != launch["remote"] or settings["engine_sha256"] != proof["engine_sha256"]
            or completion["settings_sha256"] != uploads["settings.json"]
            or completion["status"] != "completed_requires_independent_verification"):
        raise ValueError("execution trace settings or completion differs")
    limited(completion)
    ancestor = read(parent / "launch.json")
    parent_service = read(parent / "archive/immediate/engine/process.json")["phase_snapshot_engine"]["service_sha256"]
    if (launch["frozen_runtime_sha256"] != ancestor["frozen_runtime_sha256"]
            or settings["service_sha256"] != parent_service):
        raise ValueError("execution trace frozen runtime parent differs")
    calibration, first_process, _ = validate_native(archive / "calibration", "calibration", settings,
        launch["frozen_runtime_sha256"])
    _, second_process, result = validate_native(archive / "trace", "trace", settings,
        launch["frozen_runtime_sha256"], center=calibration["opening"]["libg_base"])
    if (first_process["child_pid"], first_process["child_start_ticks"]) == (
            second_process["child_pid"], second_process["child_start_ticks"]):
        raise ValueError("execution trace second native process reused")
    return dict(schema="FairFinalRandomExecutionVerification/v1", status="verified_limited_scope",
        launch_sha256=launch_sha, receipt_sha256=receipt_sha, verifier_sha256=sha(__file__),
        engine_sha256=settings["engine_sha256"], native_processes=2, archived_files=len(files), **result,
        other_algorithms_covered=False, hidden_random_coverage_closed=False,
        training_admissible=False, gates_closed=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    parser.add_argument("--parent", type=Path, required=True)
    arguments = parser.parse_args()
    result = verify(arguments.attempt, arguments.launch_sha, arguments.receipt_sha, parent=arguments.parent)
    with (arguments.attempt / "verification.json").open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
