import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import selected_collection_worker as worker


def write_json(path, value):
    path.write_text(json.dumps(value))
    return worker.sha(path)


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    deployment = tmp_path / "deployment"
    integration = deployment / worker.INTEGRATION
    integration.mkdir(parents=True)
    monkeypatch.setattr(worker, "ROOT", integration)
    monkeypatch.setattr(worker, "process_identity", lambda process: dict(pid=process.pid, start_ticks="fixture"))
    collector = integration / "collect_selected.py"
    collector.write_text("""import json,sys,time
from pathlib import Path
root=Path(sys.argv[1])
mode=sys.argv[2]
if '--preflight-only' in sys.argv:
 print('preflight passed',flush=True)
 if mode=='preflight_failure':raise SystemExit(7)
 if mode=='preflight_timeout':time.sleep(30)
else:
 if not (root/'engine-ready.json').exists():raise ValueError('engine not attested before collection')
 if mode=='collection_failure':raise SystemExit(9)
 if mode in ('collection_timeout','interrupt'):time.sleep(30)
 if mode=='missing_report':raise SystemExit(0)
 report=root/'data/attempts/match/1/report.json'
 report.parent.mkdir(parents=True)
 report.write_text(json.dumps(dict(fixture=True)))
""")
    (integration / "selected_collection_worker.py").write_bytes(Path(worker.__file__).read_bytes())
    launcher = deployment / "phase_snapshot_launcher.py"
    launcher.write_text("""import argparse,hashlib,json,os,signal,socket,time
from pathlib import Path
parser=argparse.ArgumentParser()
for name in ('project','port','instance','probe','engine-sha256'):parser.add_argument('--'+name,required=True)
args=parser.parse_args()
instance=Path(args.instance);instance.mkdir()
service=Path(args.project)/'research/standalone-resident-p18-stepatomic/service.py'
record=dict(pid=os.getpid(),start_ticks='fixture',ready=True,phase='ready',
 phase_snapshot_engine=dict(path=str(Path(args.probe).resolve()),sha256=args.engine_sha256,
 launcher_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
 service_sha256=hashlib.sha256(service.read_bytes()).hexdigest()))
mode=(Path(args.project).parent/'engine-mode').read_text() if (Path(args.project).parent/'engine-mode').exists() else ''
if mode=='attestation':record['phase_snapshot_engine']['sha256']='0'*64
if mode=='startup_timeout':record['ready']=False
listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
listener.bind(('127.0.0.1',int(args.port)));listener.listen()
(instance/'process.json').write_text(json.dumps(record))
def stop(*unused):
 listener.close()
 record.update(phase='stopped',ready=False)
 if mode=='unclean':record['phase']='failed'
 (instance/'process.json').write_text(json.dumps(record))
 raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
while True:time.sleep(.05)
""")
    service = deployment / "project/research/standalone-resident-p18-stepatomic/service.py"
    service.parent.mkdir(parents=True)
    service.write_text("fixture engine service")
    probe = deployment / "probe.so"
    probe.write_bytes(b"fixture native engine")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    root = tmp_path / "run"
    request = dict(schema="FairFinalSelectedCollectionRun/v1", collector_args=[str(root), "normal"],
        engine=dict(project=str(deployment / "project"), probe=str(probe), launcher=str(launcher),
            launcher_sha256=worker.sha(launcher), service_sha256=worker.sha(service)))
    request_path = tmp_path / "request.json"
    identity = {str(path.relative_to(deployment)): worker.sha(path) for path in
        (collector, integration / "selected_collection_worker.py", launcher)}
    deployment_sha = write_json(deployment / "deployment.json", identity)
    monkeypatch.setattr(worker, "validate_request", lambda *unused, **kwargs:
        SimpleNamespace(port=port, engine_sha=worker.sha(probe), match_id="match", attempt_id=1))
    return dict(root=root, deployment=deployment, request=request, request_path=request_path,
        deployment_sha=deployment_sha, port=port)


def execute(experiment, **options):
    options = dict(preflight_timeout=10, startup_timeout=10) | options
    return worker.run(experiment["root"], request_path=experiment["request_path"],
        request_sha=write_json(experiment["request_path"], experiment["request"]),
        deployment_root=experiment["deployment"], deployment_sha=experiment["deployment_sha"],
        **options)


def assert_reaped(process):
    with pytest.raises(ProcessLookupError):
        os.kill(process["pid"], 0)


def test_preflight_then_attested_engine_collection_and_cleanup(experiment):
    execute(experiment)
    root = experiment["root"]
    completion = json.loads((root / "completion.json").read_text())
    assert completion["status"] == "collected_requires_independent_verification"
    assert completion["training_admissible"] is completion["formal_g2_pass"] is False
    cleanup = json.loads((root / "engine-cleanup.json").read_text())
    assert cleanup["clean"] is cleanup["port_released"] is True
    for name in ("preflight-process.json", "collection-process.json"):
        result = json.loads((root / name).read_text())
        assert result["exit_code"] == 0
        assert_reaped(result["process"])
    assert_reaped(cleanup["launcher"])
    worker.check_port(experiment["port"])
    with pytest.raises(FileExistsError):
        execute(experiment)


@pytest.mark.parametrize("mode", ["preflight_failure", "collection_failure", "collection_timeout"])
def test_failures_do_not_publish_completion_and_owned_processes_are_reaped(experiment, mode):
    experiment["request"]["collector_args"][1] = mode
    with pytest.raises((RuntimeError, TimeoutError)):
        execute(experiment, collection_timeout=1)
    root = experiment["root"]
    assert (root / "failure.json").exists()
    assert not (root / "completion.json").exists()
    if mode == "preflight_failure":
        assert not (root / "engine-process.json").exists()
        assert not (root / "engine.log").exists()
    else:
        cleanup = json.loads((root / "engine-cleanup.json").read_text())
        assert cleanup["clean"] is True
        assert_reaped(cleanup["launcher"])
        assert_reaped(json.loads((root / "collection-process.json").read_text())["process"])
    worker.check_port(experiment["port"])


@pytest.mark.parametrize("damage", ["request", "deployment", "source", "worker_location", "timeout"])
def test_identity_or_budget_failure_precedes_any_process(experiment, monkeypatch, damage):
    options = {}
    if damage == "request":
        monkeypatch.setattr(worker, "sha", lambda unused: "0" * 64)
    elif damage == "deployment":
        experiment["deployment_sha"] = "0" * 64
    elif damage == "source":
        (experiment["deployment"] / worker.INTEGRATION / "collect_selected.py").write_text("changed")
    elif damage == "worker_location":
        monkeypatch.setattr(worker, "ROOT", experiment["deployment"])
    else:
        options["collection_timeout"] = 0
    with pytest.raises(ValueError):
        execute(experiment, **options)
    assert not experiment["root"].exists()


def test_running_port_is_not_stopped(experiment):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", experiment["port"]))
        listener.listen()
        with pytest.raises(OSError):
            execute(experiment)
        assert listener.fileno() >= 0
        assert not (experiment["root"] / "engine-process.json").exists()


@pytest.mark.parametrize("mode", ["preflight_timeout", "startup_timeout", "attestation", "unclean", "missing_report"])
def test_incomplete_lifecycle_records_failure_without_completion(experiment, mode):
    if mode in ("preflight_timeout", "missing_report"):
        experiment["request"]["collector_args"][1] = mode
    else:
        (experiment["deployment"] / "engine-mode").write_text(mode)
    with pytest.raises((TimeoutError, ValueError)):
        execute(experiment, preflight_timeout=1, startup_timeout=1)
    root = experiment["root"]
    assert (root / "failure.json").exists()
    assert not (root / "completion.json").exists()
    if mode == "preflight_timeout":
        assert not (root / "engine-process.json").exists()
        assert_reaped(json.loads((root / "preflight-process.json").read_text())["process"])
    else:
        cleanup = json.loads((root / "engine-cleanup.json").read_text())
        assert cleanup["clean"] is (mode != "unclean")
        assert_reaped(cleanup["launcher"])
    worker.check_port(experiment["port"])


def test_interrupt_during_collection_cleans_engine_and_child(experiment, monkeypatch):
    experiment["request"]["collector_args"][1] = "interrupt"
    run_phase = worker.run_phase

    def interrupted(root, phase, command, **options):
        if phase != "collection":
            return run_phase(root, phase, command, **options)
        timer = threading.Timer(.2, lambda: os.kill(os.getpid(), signal.SIGINT))
        timer.start()
        try:
            return run_phase(root, phase, command, **options)
        finally:
            timer.cancel()

    monkeypatch.setattr(worker, "run_phase", interrupted)
    with pytest.raises(KeyboardInterrupt):
        execute(experiment)
    root = experiment["root"]
    assert (root / "failure.json").exists()
    assert not (root / "completion.json").exists()
    cleanup = json.loads((root / "engine-cleanup.json").read_text())
    assert cleanup["clean"] is True
    assert_reaped(cleanup["launcher"])
    assert_reaped(json.loads((root / "collection-process.json").read_text())["process"])
    worker.check_port(experiment["port"])


def test_request_parser_requires_explicit_output_and_engine_identity(tmp_path, monkeypatch):
    from collect_selected import argument_parser

    deployment = tmp_path.resolve()
    root = deployment / "run"
    launcher = deployment / "phase_snapshot_launcher.py"
    launcher.write_text("fixture")
    service = deployment / "project/research/standalone-resident-p18-stepatomic/service.py"
    service.parent.mkdir(parents=True)
    service.write_text("fixture")
    probe = deployment / "probe.so"
    probe.write_text("fixture")
    arguments = ["--engineering-acceptance", "--port", "29731", "--output", str(root / "data")]
    for name in ("private-plan", "seed-registration", "excluded-seed-inventories", "seed-source-map",
            "seed-source-artifacts", "parent-data-roots", "source", "selected", "loss-weights", "selection-config"):
        arguments += ["--" + name, str(deployment / name)]
    for name in ("plan-sha", "seed-registration-sha", "selected-sha", "match-id"):
        arguments += ["--" + name, "0" * 64]
    arguments += ["--engine-sha", worker.sha(probe)]
    argument_parser().parse_args(arguments)
    request = dict(schema="FairFinalSelectedCollectionRun/v1", collector_args=arguments,
        engine=dict(project=str(deployment / "project"), probe=str(probe), launcher=str(launcher),
            launcher_sha256=worker.sha(launcher), service_sha256=worker.sha(service)))
    options = worker.validate_request(request, root=root, deployment_root=deployment)
    assert options.port == 29731
    for changed in ("output", "engine", "preflight"):
        modified = json.loads(json.dumps(request))
        if changed == "output":
            modified["collector_args"][modified["collector_args"].index("--output") + 1] = str(deployment)
        elif changed == "engine":
            modified["engine"]["service_sha256"] = "0" * 64
        else:
            modified["collector_args"].append("--preflight-only")
        with pytest.raises(ValueError):
            worker.validate_request(modified, root=root, deployment_root=deployment)
