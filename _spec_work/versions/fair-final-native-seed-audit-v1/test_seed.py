from copy import deepcopy
import json
from pathlib import Path

import pytest

from seed_audit import LIBG_SHA, PROBE_SHA, REGIONS, seed_effect, sha, static_report
from verify_seed import LIMITS, semantic, validate, validate_census, validate_pair, validate_probe


@pytest.mark.parametrize("failed", [False, True])
def test_repeated_reset_replaces_atomic_transport_before_warmup(monkeypatch, failed):
    import probe_seed
    events = []
    class Transport:
        def __init__(self, name):
            self.name = name
        def close_transport(self):
            events.append((self.name, "close"))
        def start_atomic_steps(self):
            events.append((self.name, "atomic"))
    original, replacement = Transport("original"), Transport("replacement")
    def create(port, *, transport):
        assert (port, transport) == (29775, "compact-delta")
        events.append(("replacement", "create"))
        return replacement
    class Environment:
        def __init__(self, *, native):
            assert native is replacement
        def reset(self, *, match_config, options):
            assert (match_config, options) == ("config", "options")
            events.append(("replacement", "warmup"))
            if failed:
                raise ValueError("reset failed")
    monkeypatch.setattr(probe_seed, "create_search_native", create)
    monkeypatch.setattr(probe_seed, "FinalBattleEnv", Environment)
    if failed:
        with pytest.raises(ValueError, match="reset failed"):
            probe_seed.reset_transport(original, 29775, "config", "options")
    else:
        native, environment = probe_seed.reset_transport(original, 29775, "config", "options")
        assert native is replacement and isinstance(environment, Environment)
    assert events == [("original", "close"), ("replacement", "create"),
        ("replacement", "warmup"), ("replacement", "close" if failed else "atomic")]


@pytest.mark.parametrize("cached,increment", [(100, 0), (115, 0), (116, 1), (99, 1)])
def test_unsigned_time_threshold(cached, increment):
    assert seed_effect(17, 100, cached)["effective_seed"] == 17 + increment


def test_uint32_seed_wraps_and_zero_cache_samples_once():
    assert seed_effect(2**32 - 1, 100, 0, sampled_time=116)["effective_seed"] == 0
    assert seed_effect(17, 100, 115)["cached_time"] == 115
    with pytest.raises(ValueError):
        seed_effect(17, 100, 115, sampled_time=200)
    with pytest.raises(ValueError):
        seed_effect(17, 100, 0)


@pytest.mark.parametrize("values", [(True, 100, 100), (-1, 100, 100), (2**32, 100, 100),
    (1, -1, 100), (1, 100, 2**64), (1, 100, True)])
def test_seed_input_identity(values):
    with pytest.raises(ValueError):
        seed_effect(*values)


def census(tick=0, pid=10):
    return dict(generation=1, stateEpoch=1, tick=tick, base_time=100, cached_time=101,
        manager_rng=123, increment=0, child_pid=pid, child_start_ticks="20", readonly=True,
        training_admissible=False, gates_closed=False)


def probe(delayed=False, pid=10):
    startup = census(pid=pid)
    startup["manager_rng"] = None
    initial = dict(digest="initial", census=census(pid=pid))
    repeated = deepcopy(initial)
    repeated["census"]["generation"] = 2
    rows = []
    for tick in (90, 1200, 3600, 5400):
        continuation = dict(digest=f"next-{tick}", census=semantic(census(tick + 1, pid)))
        rows.append(dict(threshold=tick, census=census(tick, pid), restored=census(tick, pid),
            digest=f"stage-{tick}", restored_digest=f"stage-{tick}",
            continuations=[deepcopy(continuation), deepcopy(continuation)],
            stale_rejected=["generation", "stateEpoch", "tick"], snapshot_released=True))
    return dict(schema="fair-final-native-seed-probe.v1", status="passed_limited_scope",
        delayed=delayed, behavior="legal-WAIT-only", startup=startup, before_reset=deepcopy(startup),
        reset_wall_time=117 if delayed else 102, initial=initial, repeated_reset=repeated, records=rows,
        **{name: False for name in LIMITS})


def test_lifecycle_and_cross_process_reference():
    first, second = probe(), probe(True, 11)
    for report in (first, second):
        validate_probe(report, dict(child_pid=report["initial"]["census"]["child_pid"],
            child_start_ticks="20"), delayed=report["delayed"])
    validate_pair(first, second)


@pytest.mark.parametrize("field,value", [("readonly", False), ("manager_rng", True),
    ("cached_time", 0), ("base_time", 0), ("increment", True), ("increment", 1),
    ("child_pid", 11), ("child_start_ticks", "21"), ("training_admissible", True)])
def test_census_rejects_invalid_identity(field, value):
    record = census()
    record[field] = value
    with pytest.raises(ValueError):
        validate_census(record, dict(child_pid=10, child_start_ticks="20"), initialized=True)


@pytest.mark.parametrize("mutation", ["delay", "reset", "counter", "stage", "release", "restore",
    "continuation", "stale", "global", "scope"])
def test_probe_rejects_broken_lifecycle(mutation):
    report = probe(True)
    if mutation == "delay":
        report["reset_wall_time"] = 115
    elif mutation == "reset":
        report["repeated_reset"]["digest"] = "different"
    elif mutation == "counter":
        report["repeated_reset"]["census"]["generation"] = 1
    elif mutation == "stage":
        report["records"].pop()
    elif mutation == "release":
        report["records"][0]["snapshot_released"] = False
    elif mutation == "restore":
        report["records"][0]["restored_digest"] = "different"
    elif mutation == "continuation":
        report["records"][0]["continuations"][1]["digest"] = "different"
    elif mutation == "stale":
        report["records"][0]["stale_rejected"].pop()
    elif mutation == "global":
        for record in (report["records"][0]["census"], report["records"][0]["restored"]):
            record["cached_time"] = 102
    else:
        report["hidden_random_coverage_closed"] = True
    with pytest.raises(ValueError):
        validate_probe(report, dict(child_pid=10, child_start_ticks="20"), delayed=True)


@pytest.mark.parametrize("mutation", ["process", "initial", "stage", "rng", "increment"])
def test_cross_process_rejects_divergence(mutation):
    first, second = probe(), probe(True, 11)
    if mutation == "process":
        second["initial"]["census"]["child_pid"] = 10
    elif mutation == "initial":
        second["initial"]["digest"] = "different"
    elif mutation == "stage":
        second["records"][2]["digest"] = "different"
    else:
        second["records"][2]["census"]["manager_rng" if mutation == "rng" else "increment"] += 1
    with pytest.raises(ValueError):
        validate_pair(first, second)


def test_static_registration_and_all_instruction_regions():
    root = Path(__file__).resolve().parent
    recorded = json.loads((root / "static-report.json").read_text())
    assert recorded["libg_sha256"] == LIBG_SHA
    assert recorded["constructor_slot"] == "0x18e5278"
    assert recorded["time_import_slot"] == "0x18e96f0"
    assert {name: row["sha256"] for name, row in recorded["regions"].items()} == {
        name: region[2] for name, region in REGIONS.items()}
    library = root.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not library.exists():
        pytest.skip("private native library omitted from public snapshot")
    assert static_report(library) == recorded


def archived_fixture(directory):
    root = Path(__file__).resolve().parent
    archive = directory / "archive"
    archive.mkdir()
    files = {}
    def save(name, value):
        target = archive / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            target.write_bytes(value)
        else:
            target.write_text(json.dumps(value))
        files[name] = sha(target)
    sources = {name: sha(root / name) for name in ("seed_audit.py", "probe_seed.py", "seed_worker.py")}
    settings = dict(port=29775, engine_sha256=PROBE_SHA, source_sha256=sources, probe="fixture-probe")
    for name in sources:
        save(name, (root / name).read_bytes())
    save("settings.json", settings)
    uploaded = dict(files)
    for variant, delayed, pid in (("immediate", False, 10), ("delayed", True, 11)):
        report = probe(delayed, pid)
        report["source_sha256"] = {"frozen": "source"}
        process = dict(child_pid=pid, child_start_ticks="20", phase="stopped",
            phase_snapshot_engine=dict(path="fixture-probe", sha256=PROBE_SHA))
        save(variant + "/report.json", report)
        save(variant + "/progress.json", dict(records=report["records"]))
        save(variant + "/engine/process.json", process)
        save(variant + "/cleanup.json", dict(exit_code=0, error=None, clean=True, listening=False,
            pids_alive=dict(pid=False, child_pid=False), port=29775, process=process))
    launch = dict(schema="fair-final-native-seed-launch.v1", uploaded_sha256=uploaded, settings=settings,
        runner_sha256=sha(root / "run_seed.py"), static_report_sha256=sha(root / "static-report.json"),
        frozen_runtime_sha256={"frozen": "source"}, training_admissible=False, gates_closed=False)
    (directory / "launch.json").write_text(json.dumps(launch))
    receipt = dict(schema="fair-final-native-seed-download.v1", source_worker_stopped=True,
        launch_sha256=sha(directory / "launch.json"), files=files, training_admissible=False, gates_closed=False)
    (directory / "download.json").write_text(json.dumps(receipt))
    return sha(directory / "launch.json"), sha(directory / "download.json")


def test_archive_end_to_end_fixture(tmp_path):
    report = validate(tmp_path, *archived_fixture(tmp_path))
    assert report["status"] == "passed_limited_scope"
    assert report["gates_closed"] is False


@pytest.mark.parametrize("mutation", ["launch", "receipt", "missing", "payload", "failure", "cleanup",
    "source", "scope", "path", "stopped"])
def test_archive_rejects_tampering(tmp_path, mutation):
    launch_sha, receipt_sha = archived_fixture(tmp_path)
    if mutation in ("launch", "receipt"):
        target = tmp_path / ("launch.json" if mutation == "launch" else "download.json")
        target.write_text(target.read_text() + " ")
    elif mutation == "payload":
        (tmp_path / "archive/immediate/report.json").write_text("{}")
    else:
        target = tmp_path / "download.json"
        receipt = json.loads(target.read_text())
        if mutation == "missing":
            receipt["files"].pop("delayed/report.json")
        elif mutation == "failure":
            receipt["files"]["delayed/failure.json"] = "failure"
        elif mutation == "cleanup":
            path = tmp_path / "archive/delayed/cleanup.json"
            cleanup = json.loads(path.read_text())
            cleanup["clean"] = False
            path.write_text(json.dumps(cleanup))
            receipt["files"]["delayed/cleanup.json"] = sha(path)
        elif mutation == "source":
            receipt["files"]["seed_audit.py"] = "different"
        elif mutation == "scope":
            receipt["gates_closed"] = True
        elif mutation == "path":
            receipt["files"]["../outside.json"] = "outside"
        else:
            receipt["source_worker_stopped"] = False
        target.write_text(json.dumps(receipt))
        receipt_sha = sha(target)
    with pytest.raises(ValueError):
        validate(tmp_path, launch_sha, receipt_sha)
