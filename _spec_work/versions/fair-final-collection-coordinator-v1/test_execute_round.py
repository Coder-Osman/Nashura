import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import execute_round as executor
import preflight_round as preflight
from test_preflight_round import round_case, execute as prepare, write
from test_seed_registration import registration_case


@pytest.fixture
def execution_case(round_case, tmp_path, monkeypatch):
    case = round_case
    control = tmp_path / "control.json"
    launches = tmp_path / "launches.jsonl"
    write(control, {})
    worker = case["deployment_root"] / (executor.INTEGRATION + "selected_collection_worker.py")
    worker.write_text("import json,sys,time\nfrom pathlib import Path\n"
        "def value(flag):return sys.argv[sys.argv.index(flag)+1]\n"
        "root=Path(value('--root'));request=json.loads(Path(value('--request')).read_text())\n"
        "args=request['collector_args'];match=args[args.index('--match-id')+1]\n"
        "control=json.loads(Path(" + repr(str(control)) + ").read_text())\n"
        "with Path(" + repr(str(launches)) + ").open('a') as stream:stream.write(json.dumps(match)+'\\n')\n"
        "root.mkdir();(root/'request.json').write_text(json.dumps(request))\n"
        "time.sleep(control.get('delay',0.05))\n"
        "if control.get('worker_failure')==match:raise SystemExit(7)\n"
        "(root/'completion.json').write_text(json.dumps({'fixture':True}))\n"
        "(root/'episode.json').write_text(json.dumps({'rows':2,'fixture':True}))\n")
    verifier = case["deployment_root"] / (executor.INTEGRATION + "verify_selected_collection.py")
    verifier.write_text("import hashlib,json,sys,time\nfrom pathlib import Path\n"
        "def value(flag):return sys.argv[sys.argv.index(flag)+1]\n"
        "root=Path(sys.argv[1]);request=json.loads((root/'request.json').read_text())\n"
        "args=request['collector_args'];match=args[args.index('--match-id')+1]\n"
        "control=json.loads(Path(" + repr(str(control)) + ").read_text())\n"
        "if control.get('verify_failure')==match:raise SystemExit(9)\n"
        "time.sleep(control.get('verify_delay',0))\n"
        "assert (root/'completion.json').is_file() and (root/'episode.json').is_file()\n"
        "report=dict(schema='FairFinalSelectedCollectionVerification/v1',"
        "status='passed_archived_collection_content_scope',match_id=match,round=2,rows=2,"
        "receipt_sha='f'*64,request_sha256=value('--request-sha'),deployment_sha256=value('--deployment-sha'),"
        "completion_sha256=value('--completion-sha'),selected_checkpoint_sha=value('--selected-sha'),"
        "verifier_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),terminal_tick=42,"
        "teacher_targets_recomputed=False,scope='archived_source_process_terminal_episode_seed_and_selected_parent_consistency',"
        "training_admissible=False,formal_g2_pass=False,gates_closed=False)\n"
        "if control.get('scope'):report['training_admissible']=True\n"
        "Path(value('--output')).write_text(json.dumps(report))\n")
    manifest = case["deployment_root"] / "deployment.json"
    identity = preflight.read_json(manifest)
    for path in (worker, verifier):
        identity[path.relative_to(case["deployment_root"]).as_posix()] = preflight.file_identity(path)["sha256"]
    case["deployment_sha"] = write(manifest, identity)
    prepare(case)
    selected_report = tmp_path / "selection-report.json"
    write(selected_report, dict(checkpoint_sha=preflight.file_identity(case["selected"])["sha256"]))
    monkeypatch.setattr(executor, "process_identity", lambda process: dict(pid=process.pid, start_ticks=str(process.pid)))
    monkeypatch.setattr(executor, "host_identity", lambda: dict(hostname="fixture", boot_id="fixture-boot"))

    def alive(pid, start_ticks):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    monkeypatch.setattr(executor, "matching_process", alive)
    return dict(case=case, control=control, launches=launches,
        prepared_path=case["output"] / "prepared.json",
        prepared_sha=preflight.file_identity(case["output"] / "prepared.json")["sha256"],
        selected_report_path=selected_report, selected_report_sha=preflight.file_identity(selected_report)["sha256"],
        output=tmp_path / "execution")


def execute(case, **options):
    return executor.execute(**{key: case[key] for key in ("prepared_path", "prepared_sha",
        "selected_report_path", "selected_report_sha", "output")} | options)


def launches(case):
    return [json.loads(line) for line in case["launches"].read_text().splitlines()]


def test_complete_round_and_repeat_reverify_without_relaunch(execution_case):
    case = execution_case
    result = execute(case)
    assert result["status"] == "passed_archived_complete_round_scope"
    assert result["rows"] == 12 and len(result["tasks"]) == 6
    assert all(result[key] is False for key in executor.SCOPE)
    assert result == preflight.read_json(case["output"] / "complete.json")
    assert execute(case) == result
    assert len(launches(case)) == len(set(launches(case))) == 6
    assert len(list((case["output"] / "invocations").glob("*/*-verification.json"))) == 12


def test_partial_verification_failure_resumes_without_recollecting(execution_case):
    case = execution_case
    matches = [task["match_id"] for task in preflight.read_json(case["prepared_path"])["tasks"]]
    write(case["control"], dict(verify_failure=matches[1]))
    with pytest.raises(subprocess.CalledProcessError):
        execute(case)
    assert launches(case) == matches[:2]
    assert not (case["output"] / "complete.json").exists()
    assert (case["output"] / "invocations/1/failure.json").is_file()
    write(case["control"], {})
    assert len(execute(case)["tasks"]) == 6
    assert launches(case) == matches


def test_crash_after_worker_exit_recovers_completed_archive(execution_case, monkeypatch):
    case = execution_case
    original = executor.run_worker

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(executor, "run_worker", interrupted)
    with pytest.raises(KeyboardInterrupt):
        execute(case)
    assert len(launches(case)) == 1
    monkeypatch.setattr(executor, "run_worker", original)
    execute(case)
    assert len(launches(case)) == 6


def test_worker_failure_is_quarantined_without_new_attempt(execution_case):
    case = execution_case
    match = preflight.read_json(case["prepared_path"])["tasks"][0]["match_id"]
    write(case["control"], dict(worker_failure=match))
    with pytest.raises(RuntimeError, match="worker failed"):
        execute(case)
    write(case["control"], {})
    with pytest.raises(ValueError, match="quarantined"):
        execute(case)
    assert launches(case) == [match]
    assert not (case["output"] / "complete.json").exists()


@pytest.mark.parametrize("damage", ["prepared", "source", "report", "verifier", "command", "scope", "coverage", "output_overlap"])
def test_invalid_frozen_identity_rejected_before_worker(execution_case, damage):
    case = execution_case
    if damage == "prepared":
        case["prepared_sha"] = "0" * 64
    elif damage == "source":
        case["case"]["source"].write_bytes(b"changed")
    elif damage == "report":
        write(case["selected_report_path"], dict(checkpoint_sha="0" * 64))
    elif damage == "verifier":
        (case["case"]["deployment_root"] / (executor.INTEGRATION + "verify_selected_collection.py")).write_text("raise SystemExit(0)\n")
    elif damage == "output_overlap":
        case["output"] = case["case"]["parents"][0] / "execution"
    else:
        prepared = preflight.read_json(case["prepared_path"])
        if damage == "command":
            prepared["tasks"][0]["worker_command"].append("--extra")
        elif damage == "scope":
            prepared["gates_closed"] = 0
        else:
            prepared["tasks"].pop()
        case["prepared_sha"] = write(case["prepared_path"], prepared)
    with pytest.raises((ValueError, KeyError)):
        execute(case)
    assert not case["launches"].exists()


@pytest.mark.parametrize("damage", ["archive", "new_file", "accepted", "completion", "link"])
def test_completed_archives_reverified_on_resume(execution_case, damage):
    case = execution_case
    result = execute(case)
    first = result["tasks"][0]
    root = Path(first["root"])
    if damage == "archive":
        (root / "episode.json").write_text('{"rows":999}')
    elif damage == "new_file":
        (root / "extra.json").write_text("{}")
    elif damage == "accepted":
        write(case["output"] / "tasks" / first["match_id"] / "accepted.json", {})
    elif damage == "completion":
        (root / "completion.json").unlink()
    else:
        (root / "linked").symlink_to(case["selected_report_path"])
    with pytest.raises((ValueError, FileNotFoundError)):
        execute(case)
    assert len(launches(case)) == 6


def test_prepared_inode_lock_blocks_second_output_owner(execution_case):
    case = execution_case
    with executor.exclusive(case["prepared_path"]):
        with pytest.raises(RuntimeError, match="already owned"):
            execute(case)
    assert not case["launches"].exists()


@pytest.mark.parametrize("damage", ["live", "unknown_launch", "unowned_root", "missing_completion"])
def test_uncertain_worker_is_not_relaunched(execution_case, monkeypatch, damage):
    case = execution_case
    original = executor.run_worker

    def interrupted(command, state, **kwargs):
        if damage == "unknown_launch":
            state.mkdir(parents=True)
            executor.record(state / "intent.json", dict(command=command))
        else:
            original(command, state, **kwargs)
        raise KeyboardInterrupt()

    if damage == "unowned_root":
        case["case"]["roots"][0].mkdir()
    else:
        monkeypatch.setattr(executor, "run_worker", interrupted)
        with pytest.raises(KeyboardInterrupt):
            execute(case)
        monkeypatch.setattr(executor, "run_worker", original)
    if damage == "live":
        monkeypatch.setattr(executor, "matching_process", lambda *args: True)
    elif damage == "missing_completion":
        (case["case"]["roots"][0] / "completion.json").unlink()
    before = launches(case) if case["launches"].exists() else []
    with pytest.raises((ValueError, RuntimeError, FileNotFoundError)):
        execute(case)
    assert (launches(case) if case["launches"].exists() else []) == before


def test_independent_verifier_cannot_elevate_scope(execution_case):
    case = execution_case
    write(case["control"], dict(scope=True))
    with pytest.raises(ValueError, match="verification identity or scope"):
        execute(case)
    assert len(launches(case)) == 1
    assert not (case["output"] / "complete.json").exists()


def test_independent_verification_timeout_stops_round(execution_case):
    case = execution_case
    write(case["control"], dict(verify_delay=3))
    with pytest.raises(subprocess.TimeoutExpired):
        execute(case, verification_timeout=1)
    assert len(launches(case)) == 1
    assert not (case["output"] / "complete.json").exists()


def test_later_collection_cannot_mutate_previously_accepted_archive(execution_case, monkeypatch):
    case = execution_case
    original = executor.verify_task
    calls = []

    def verify(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result["match_id"])
        if len(calls) == 6:
            (case["case"]["roots"][0] / "late.json").write_text("{}")
        return result

    monkeypatch.setattr(executor, "verify_task", verify)
    with pytest.raises(ValueError, match="before round commit"):
        execute(case)
    assert not (case["output"] / "complete.json").exists()


def test_reboot_or_different_host_requires_explicit_handoff(execution_case, monkeypatch):
    case = execution_case
    execute(case)
    monkeypatch.setattr(executor, "host_identity", lambda: dict(hostname="fixture", boot_id="new-boot"))
    with pytest.raises(ValueError, match="immutable artifact identity conflict"):
        execute(case)
    assert len(launches(case)) == 6


@pytest.mark.parametrize("interruption", ["timeout", "interrupt"])
def test_interrupted_worker_is_signalled_and_reaped(execution_case, monkeypatch, interruption):
    case = execution_case
    task = preflight.read_json(case["prepared_path"])["tasks"][0]
    write(case["control"], dict(delay=5))
    state = case["output"] / "isolated-worker"
    if interruption == "interrupt":
        original = executor.subprocess.Popen.wait
        waits = []

        def interrupted(process, *args, **kwargs):
            waits.append(process.pid)
            if len(waits) == 1:
                raise KeyboardInterrupt()
            return original(process, *args, **kwargs)

        monkeypatch.setattr(executor.subprocess.Popen, "wait", interrupted)
    with pytest.raises(KeyboardInterrupt if interruption == "interrupt" else TimeoutError):
        executor.run_worker(task["worker_command"], state, timeout=.2)
    identity = preflight.read_json(state / "process.json")
    assert not executor.matching_process(identity["pid"], identity["start_ticks"])
    assert preflight.read_json(state / "exit.json")["exit_code"] != 0


@pytest.mark.parametrize("timeout", [0, -1, 14401, True, 1.5])
def test_execution_budgets_are_bounded(execution_case, timeout):
    with pytest.raises(ValueError):
        execute(execution_case, collection_timeout=timeout)
    assert not execution_case["launches"].exists()
