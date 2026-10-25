import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import preflight_round as preflight
from native_runner.training.fair_final.storage import durable_write, ensure_immutable, json_bytes
from native_runner.training.fair_search.contracts import semantic_digest, require_sha
from selected_collection_worker import process_identity, matching_process


INTEGRATION = "_spec_work/versions/fair-final-migration-integration-v1/"
SCOPE = dict(training_admissible=False, formal_g2_pass=False, gates_closed=False)


def record(path, value):
    ensure_immutable(path, json_bytes(value))


def host_identity():
    return dict(hostname=os.uname().nodename,
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip())


@contextmanager
def exclusive(path, *, create=False):
    if path.is_symlink():
        raise ValueError("symbolic lock path")
    with path.open("a+b" if create else "rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("collection round already owned by another coordinator") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def tree_identity(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("regular collection archive required")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symbolic collection archive entry")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = preflight.file_identity(path)
        elif not path.is_dir():
            raise ValueError("nonregular collection archive entry")
    return result


def validate_prepared(path, expected_sha):
    if preflight.file_identity(path)["sha256"] != require_sha(expected_sha):
        raise ValueError("prepared round checksum differs")
    prepared = preflight.read_json(path)
    if (prepared.get("schema") != "FairFinalCollectionRoundPreflight/v1"
            or prepared.get("status") != "prepared_only"
            or prepared.get("native_collection_executed") is not False
            or any(prepared.get(key) is not False for key in SCOPE)
            or prepared.get("coordinator_sha256") != preflight.file_identity(preflight.__file__)["sha256"]):
        raise ValueError("prepared round source or scope differs")
    tasks = prepared["tasks"]
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("nonempty prepared round required")
    first_command = tasks[0]["worker_command"]
    deployment = Path(first_command[first_command.index("--deployment-root") + 1])
    identity = preflight.deployment_identity(deployment, prepared["deployment_sha256"])
    if INTEGRATION + "verify_selected_collection.py" not in identity:
        raise ValueError("independent collection verifier must be pinned in deployment")
    entries, paths = [], []
    for index, task in enumerate(tasks):
        command = task["worker_command"]
        request_path = Path(command[command.index("--request") + 1])
        entry = preflight.parse_request(request_path, task["request_sha256"], deployment)
        request, options, root, common = entry
        expected = [sys.executable, "-B", "-u", str(deployment / (INTEGRATION + "selected_collection_worker.py")),
            "--root", str(root), "--request", str(request_path), "--request-sha", task["request_sha256"],
            "--deployment-root", str(deployment), "--deployment-sha", prepared["deployment_sha256"]]
        if (command != expected or task["root"] != str(root) or task["match_id"] != options.match_id
                or (entries and common != entries[0][3])):
            raise ValueError("prepared worker command or shared inputs differ")
        log = path.parent / (str(index) + "-preflight.log")
        if preflight.file_identity(log) != task["log_identity"]:
            raise ValueError("prepared preflight log differs")
        if preflight.read_json(options.private_plan)["rounds"][-1]["manifest"]["round"] != prepared["round"]:
            raise ValueError("prepared round number differs")
        if task["preflight"]["selected_network_sha"] != prepared["selected_network_sha"]:
            raise ValueError("prepared network differs")
        paths.append(request_path)
        entries.append(entry)
    plan = preflight.read_json(entries[0][1].private_plan)
    if (len(paths) != len(set(paths)) or len({task["match_id"] for task in tasks}) != len(tasks)
            or {task["match_id"] for task in tasks} != set(plan["rounds"][-1]["manifest"]["matches"])):
        raise ValueError("prepared round task coverage differs")
    options = entries[0][1]
    if options.plan_sha != prepared["plan_sha"]:
        raise ValueError("prepared plan differs")
    evidence = preflight.validate_registration(preflight.read_json(options.seed_registration),
        expected_sha=options.seed_registration_sha, plan=plan, expected_plan_sha=options.plan_sha,
        inventory_paths=options.excluded_seed_inventories, source_map_path=options.seed_source_map,
        source_paths=options.seed_source_artifacts, match_id=options.match_id)
    for task, entry in zip(tasks, entries):
        preflight.check_preflight(task["preflight"], entry[1], evidence)
    snapshot = preflight.pinned_inputs(options, entries[0][0], paths, deployment, identity)
    if snapshot != prepared["inputs"]:
        raise ValueError("frozen prepared inputs changed")
    return prepared, entries, deployment


def run_worker(command, state, *, timeout):
    if state.exists():
        intent = preflight.read_json(state / "intent.json")
        if intent != dict(command=command):
            raise ValueError("worker execution intent differs")
        process_path = state / "process.json"
        if not process_path.exists():
            raise ValueError("interrupted launch has no owned process identity; quarantine required")
        identity = preflight.read_json(process_path)
        if (set(identity) != {"pid", "start_ticks"} or type(identity["pid"]) is not int
                or identity["pid"] <= 0 or not isinstance(identity["start_ticks"], str)
                or not identity["start_ticks"].isdigit()):
            raise ValueError("worker process identity differs")
        if matching_process(identity["pid"], identity["start_ticks"]):
            raise RuntimeError("owned worker is still alive; duplicate launch refused")
        exit_path = state / "exit.json"
        if exit_path.exists() and preflight.read_json(exit_path)["exit_code"] != 0:
            raise ValueError("failed worker attempt remains quarantined")
        return
    state.mkdir(parents=True)
    record(state / "intent.json", dict(command=command))
    with (state / "worker.log").open("xb") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1"))
        try:
            record(state / "process.json", process_identity(process))
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise TimeoutError("collection worker exceeded bounded lifecycle deadline") from error
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=100)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError("worker cleanup still pending; retain ownership and stop round") from error
            record(state / "exit.json", dict(exit_code=process.returncode))
    if code != 0:
        raise RuntimeError("collection worker failed: " + str(code))


def verify_task(task, options, deployment, deployment_sha, selected_report, invocation, timeout):
    root = Path(task["root"])
    completion_sha = preflight.file_identity(root / "completion.json")["sha256"]
    verifier = deployment / (INTEGRATION + "verify_selected_collection.py")
    output = invocation / (task["match_id"] + "-verification.json")
    command = [sys.executable, "-B", "-u", str(verifier), str(root),
        "--deployment-root", str(deployment), "--deployment-sha", deployment_sha,
        "--request-sha", task["request_sha256"], "--completion-sha", completion_sha,
        "--plan-path", str(options.private_plan), "--plan-sha", options.plan_sha,
        "--selected-path", str(options.selected), "--selected-sha", options.selected_sha,
        "--selected-report-path", str(selected_report), "--weights-path", str(options.loss_weights),
        "--config-path", str(options.selection_config), "--parent-data-roots",
        *map(str, options.parent_data_roots), "--output", str(output)]
    with output.with_suffix(".log").open("xb") as stream:
        subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
            cwd=deployment, timeout=timeout, check=True)
    report = preflight.read_json(output)
    expected = dict(schema="FairFinalSelectedCollectionVerification/v1",
        status="passed_archived_collection_content_scope", match_id=options.match_id,
        round=preflight.read_json(options.private_plan)["rounds"][-1]["manifest"]["round"],
        request_sha256=task["request_sha256"], deployment_sha256=deployment_sha,
        completion_sha256=completion_sha, selected_checkpoint_sha=options.selected_sha,
        verifier_sha256=preflight.file_identity(verifier)["sha256"], teacher_targets_recomputed=False,
        scope="archived_source_process_terminal_episode_seed_and_selected_parent_consistency", **SCOPE)
    if (any(key not in report or semantic_digest(report[key]) != semantic_digest(value)
            for key, value in expected.items()) or type(report.get("rows")) is not int
            or report["rows"] <= 0):
        raise ValueError("independent verification identity or scope differs")
    require_sha(report["receipt_sha"])
    return report


def execute(prepared_path, *, prepared_sha, selected_report_path, selected_report_sha,
        output, preflight_timeout=3600, collection_timeout=4800, startup_timeout=180,
        verification_timeout=3600):
    timeouts = (preflight_timeout, collection_timeout, startup_timeout, verification_timeout)
    if any(type(value) is not int or not 1 <= value <= 14400 for value in timeouts):
        raise ValueError("bounded integer execution deadlines required")
    prepared_path, selected_report_path = Path(prepared_path).absolute(), Path(selected_report_path).absolute()
    output = Path(output).absolute()
    with exclusive(prepared_path):
        prepared, entries, deployment = validate_prepared(prepared_path, prepared_sha)
        report_identity = preflight.file_identity(selected_report_path)
        if (report_identity["sha256"] != require_sha(selected_report_sha)
                or preflight.read_json(selected_report_path)["checkpoint_sha"] != entries[0][1].selected_sha):
            raise ValueError("selected report identity differs")
        protected = [prepared_path.parent, deployment, selected_report_path,
            *map(Path, prepared["inputs"]), *[entry[2] for entry in entries],
            *entries[0][1].parent_data_roots]
        if (output.resolve() != output or any(output.is_relative_to(path.resolve())
                or path.resolve().is_relative_to(output) for path in protected)):
            raise ValueError("execution output overlaps prepared, frozen or collection inputs")
        output.mkdir(parents=True, exist_ok=True)
        with exclusive(output / "coordinator.lock", create=True):
            binding = dict(schema="FairFinalCollectionRoundExecution/v1", prepared_sha256=prepared_sha,
                selected_report_sha256=selected_report_sha, selected_report_path=str(selected_report_path),
                executor_sha256=preflight.file_identity(__file__)["sha256"], host=host_identity(),
                timeouts=list(timeouts), **SCOPE)
            record(output / "execution.json", binding)
            invocations = output / "invocations"
            invocations.mkdir(exist_ok=True)
            invocation = invocations / str(len(list(invocations.iterdir())) + 1)
            invocation.mkdir()
            try:
                accepted = []
                for task, entry in zip(prepared["tasks"], entries):
                    validate_prepared(prepared_path, prepared_sha)
                    if preflight.file_identity(selected_report_path) != report_identity:
                        raise ValueError("frozen selected report changed")
                    state = output / "tasks" / task["match_id"]
                    command = task["worker_command"] + ["--preflight-timeout", str(preflight_timeout),
                        "--collection-timeout", str(collection_timeout), "--startup-timeout", str(startup_timeout)]
                    if not state.exists() and Path(task["root"]).exists():
                        raise ValueError("collection output exists without coordinator ownership")
                    run_worker(command, state, timeout=sum(timeouts[:3]) + 120)
                    before = tree_identity(task["root"])
                    report = verify_task(task, entry[1], deployment, prepared["deployment_sha256"],
                        selected_report_path, invocation, verification_timeout)
                    if tree_identity(task["root"]) != before:
                        raise ValueError("collection archive changed during independent verification")
                    validate_prepared(prepared_path, prepared_sha)
                    if preflight.file_identity(selected_report_path) != report_identity:
                        raise ValueError("frozen selected report changed during collection")
                    result = dict(verification=report, archive=before)
                    record(state / "accepted.json", result)
                    accepted.append(dict(match_id=task["match_id"], root=task["root"],
                        accepted_sha256=preflight.file_identity(state / "accepted.json")["sha256"],
                        rows=report["rows"], receipt_sha=report["receipt_sha"]))
                validate_prepared(prepared_path, prepared_sha)
                if preflight.file_identity(selected_report_path) != report_identity:
                    raise ValueError("frozen selected report changed before round commit")
                for task in accepted:
                    accepted_path = output / "tasks" / task["match_id"] / "accepted.json"
                    if (preflight.file_identity(accepted_path)["sha256"] != task["accepted_sha256"]
                            or tree_identity(task["root"]) != preflight.read_json(accepted_path)["archive"]):
                        raise ValueError("previously accepted collection changed before round commit")
                result = dict(schema="FairFinalCollectionRoundResult/v1",
                    status="passed_archived_complete_round_scope", round=prepared["round"],
                    execution=binding, tasks=accepted, rows=sum(task["rows"] for task in accepted),
                    teacher_targets_recomputed=False, **SCOPE)
                record(output / "complete.json", result)
                return result
            except BaseException as error:
                durable_write(invocation / "failure.json", json_bytes(dict(error=repr(error), **SCOPE)))
                raise


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--engineering-acceptance", required=True, action="store_true")
    for name in ("prepared-path", "selected-report-path", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("prepared-sha", "selected-report-sha"):
        parser.add_argument("--" + name, required=True)
    for name, default in (("preflight-timeout", 3600), ("collection-timeout", 4800),
            ("startup-timeout", 180), ("verification-timeout", 3600)):
        parser.add_argument("--" + name, type=int, default=default)
    options = vars(parser.parse_args())
    options.pop("engineering_acceptance")
    def interrupt(number, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, interrupt)
    result = execute(**options)
    print(json.dumps(dict(status=result["status"], tasks=len(result["tasks"]), **SCOPE)), flush=True)


if __name__ == "__main__":
    main()
