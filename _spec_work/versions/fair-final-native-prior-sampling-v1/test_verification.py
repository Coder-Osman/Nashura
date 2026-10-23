import json
from pathlib import Path
import random

import pytest

from prior_sampling import PORT, PROBE_SHA, REPEAT_COUNT, seeds, sha
from verify_openings import FIXED_DECK, NAMES, ROOT, SOURCES, VARIANTS, validate


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def fixture_archive(directory):
    archive = directory / "archive"
    archive.mkdir()
    remote = "/fixture/opening-diagnostic"
    for name in SOURCES:
        (archive / name).write_bytes((ROOT / name).read_bytes())
    settings = dict(root=remote, port=PORT, seeds=seeds(), repeat_count=REPEAT_COUNT,
        timeout_seconds=14400, engine_sha256=PROBE_SHA, probe="/fixture/libcrprobe.so",
        runtime_sha256={"native.py": "a" * 64},
        source_sha256={name: sha(archive / name) for name in SOURCES})
    write(archive / "settings.json", settings)
    (archive / "worker.log").write_text("")
    launch = dict(schema="fair-final-native-opening-launch.v1", settings=settings,
        remote=remote, uploaded_sha256={name: sha(archive / name) for name in (*SOURCES, "settings.json")},
        runner_sha256=sha(ROOT / "run_openings.py"), contract_sha256=sha(ROOT / "CONTRACT.md"),
        training_admissible=False, gates_closed=False)
    write(directory / "launch.json", launch)
    rng = random.Random(20261023)
    rows = []
    for ordinal, seed in enumerate(seeds()):
        players = []
        for owner in (0, 1):
            slots = list(range(8))
            rng.shuffle(slots)
            players.append(dict(owner=owner, slots=slots, cards=[FIXED_DECK[slot] for slot in slots]))
        rows.append(dict(ordinal=ordinal, seed=seed, tick=90, players=players))
    for variant in VARIANTS:
        root = archive / variant
        root.mkdir()
        selected = rows if variant == "primary" else rows[:REPEAT_COUNT]
        (root / "rows.jsonl").write_text("".join(json.dumps(row) + "\n" for row in selected))
        for name in ("engine.log", "probe.log"):
            (root / name).write_text("")
        process = dict(port=PORT, child_pid=500 if variant == "primary" else 501, phase="stopped",
            phase_snapshot_engine=dict(path=settings["probe"], sha256=PROBE_SHA))
        write(root / "engine/process.json", process)
        write(root / "cleanup.json", dict(exit_code=0, error=None, clean=True, process=process,
            pids_alive=dict(pid=False, child_pid=False), listening=False, port=PORT))
        write(root / "report.json", dict(schema="fair-final-native-opening-probe.v1", status="complete",
            variant=variant, rows_sha256=sha(root / "rows.jsonl"), source_sha256=settings["source_sha256"],
            runtime_sha256=settings["runtime_sha256"], training_admissible=False, gates_closed=False,
            seconds=10., rows=len(selected), deck=FIXED_DECK))
    refresh_receipt(directory)
    return directory


def refresh_receipt(directory):
    archive = directory / "archive"
    names = [*SOURCES, "settings.json", "worker.log",
        *(variant + "/" + name for variant in VARIANTS for name in NAMES)]
    write(directory / "download.json", dict(schema="fair-final-native-opening-download.v1",
        launch_sha256=sha(directory / "launch.json"), files={name: sha(archive / name) for name in names},
        source_worker_stopped=True, training_admissible=False, gates_closed=False))


def verify(directory):
    return validate(directory, sha(directory / "launch.json"), sha(directory / "download.json"))


def mutate(directory, name, key, value):
    path = directory / "archive" / name
    record = json.loads(path.read_text())
    record[key] = value
    write(path, record)
    refresh_receipt(directory)


def test_archive_fixture(tmp_path):
    report = verify(fixture_archive(tmp_path))
    assert report["status"] == "verified_diagnostic"
    assert report["repeat"]["repeated_seeds"] == REPEAT_COUNT
    assert report["distribution"]["status"] == "no_detected_departure_limited_scope"
    assert report["gates_closed"] is False


@pytest.mark.parametrize("name,key,value", [
    ("primary/report.json", "rows", 1023),
    ("primary/report.json", "seconds", 14400),
    ("primary/report.json", "variant", "repeat"),
    ("primary/report.json", "rows_sha256", "f" * 64),
    ("primary/report.json", "training_admissible", True),
    ("primary/report.json", "gates_closed", True),
    ("primary/report.json", "deck", list(range(8))),
    ("primary/report.json", "runtime_sha256", {}),
    ("primary/report.json", "source_sha256", {}),
    ("primary/cleanup.json", "exit_code", 124),
    ("primary/cleanup.json", "error", "timeout"),
    ("primary/cleanup.json", "clean", False),
    ("primary/cleanup.json", "listening", True),
    ("primary/cleanup.json", "pids_alive", dict(pid=False, child_pid=True)),
    ("primary/cleanup.json", "port", 29761),
    ("primary/engine/process.json", "phase", "running"),
    ("settings.json", "seeds", seeds()[::-1]),
])
def test_semantic_tampering(tmp_path, name, key, value):
    directory = fixture_archive(tmp_path)
    mutate(directory, name, key, value)
    with pytest.raises(ValueError):
        verify(directory)


def test_bad_external_identity(tmp_path):
    directory = fixture_archive(tmp_path)
    with pytest.raises(ValueError, match="external"):
        validate(directory, "0" * 64, sha(directory / "download.json"))


def test_archive_byte_tampering(tmp_path):
    directory = fixture_archive(tmp_path)
    (directory / "archive/primary/rows.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="bytes"):
        verify(directory)


def test_repeated_process_rejected(tmp_path):
    directory = fixture_archive(tmp_path)
    root = directory / "archive/repeat"
    process = json.loads((root / "engine/process.json").read_text())
    process["child_pid"] = 500
    write(root / "engine/process.json", process)
    cleanup = json.loads((root / "cleanup.json").read_text())
    cleanup["process"] = process
    write(root / "cleanup.json", cleanup)
    refresh_receipt(directory)
    with pytest.raises(ValueError, match="processes"):
        verify(directory)


def test_missing_receipt_member(tmp_path):
    directory = fixture_archive(tmp_path)
    path = directory / "download.json"
    receipt = json.loads(path.read_text())
    receipt["files"].pop("repeat/rows.jsonl")
    write(path, receipt)
    with pytest.raises(ValueError, match="coverage"):
        verify(directory)
