import contextlib
import copy
import json
from pathlib import Path
import struct

import pytest

from global_rng_audit import (
    GLOBAL_OFFSET, LIBG_SHA, STATE_SIZE, NativeGlobalRngAudit,
    classify_restore, perturb_state, state_summary, static_report)
from verify_global import validate, validate_probe


def test_perturbation_changes_only_first_bit_and_is_an_involution():
    original = bytes(range(256)) * 9 + bytes(range(200))
    assert len(original) == STATE_SIZE
    changed = perturb_state(original)
    assert changed[0] == original[0] ^ 1 and changed[1:] == original[1:]
    assert perturb_state(changed) == original
    assert classify_restore(original, changed, original) == "restored_original"
    assert classify_restore(original, changed, changed) == "retained_perturbation"
    with pytest.raises(ValueError):
        classify_restore(original, changed, original[:-1] + b"x")
    with pytest.raises(ValueError):
        classify_restore(original, original, original)


@pytest.mark.parametrize("content", [b"", b"x" * (STATE_SIZE - 1), b"x" * (STATE_SIZE + 1), bytearray(STATE_SIZE)])
def test_wrong_state_shape_is_rejected(content):
    with pytest.raises(ValueError):
        perturb_state(content)
    with pytest.raises(ValueError):
        state_summary(content)


def memory_audit(monkeypatch, *, active=False):
    import global_rng_audit

    audit = NativeGlobalRngAudit.__new__(NativeGlobalRngAudit)
    audit.pid, audit.record = 17, dict(child_start_ticks="91")
    audit.probe, audit.libg = Path("/probe"), Path("/libg")
    audit.symbols = dict(generation=0, epoch=8, tick=16, worker=20, libg=24, manager=32)
    audit._paused = lambda: contextlib.nullcontext(Path("/fake"))
    monkeypatch.setattr(Path, "read_text", lambda path: "mapping fixture")
    monkeypatch.setattr(global_rng_audit, "mapped_base", lambda maps, path: 0x100 if path == audit.probe else 0x1000)
    monkeypatch.setattr(global_rng_audit, "mapped_guest_pointer", lambda pointer, bias, maps, size: pointer + bias)
    memory = {0x100: struct.pack("<Q", 1), 0x108: struct.pack("<Q", 2),
        0x110: struct.pack("<i", 90), 0x114: bytes([active]),
        0x118: struct.pack("<Q", 0x1000), 0x120: struct.pack("<Q", 0x5000),
        0x5060: struct.pack("<i", 90), 0x1000 + GLOBAL_OFFSET: bytes(STATE_SIZE)}
    writes = []
    monkeypatch.setattr(global_rng_audit.os, "open", lambda *args: 8)
    monkeypatch.setattr(global_rng_audit.os, "close", lambda descriptor: None)
    monkeypatch.setattr(global_rng_audit.os, "pread", lambda descriptor, size, address: memory[address][:size])
    def write(descriptor, content, address):
        writes.append((address, content))
        memory[address] = content
        return len(content)
    monkeypatch.setattr(global_rng_audit.os, "pwrite", write)
    return audit, memory, writes


def test_global_memory_read_and_cas_repair(monkeypatch):
    audit, memory, writes = memory_audit(monkeypatch)
    identity = dict(generation=1, stateEpoch=2, tick=90)
    original, census = audit.global_state(identity)
    assert census["readonly"] and writes == []
    perturbed = perturb_state(original)
    assert audit.global_state(identity, expected=original, replacement=perturbed)[0] == perturbed
    assert audit.global_state(identity, expected=perturbed, replacement=original)[0] == original
    assert [address for address, content in writes] == [0x1000 + GLOBAL_OFFSET] * 2
    assert memory[0x1000 + GLOBAL_OFFSET] == original


@pytest.mark.parametrize("failure", ["generation", "stateEpoch", "tick", "active", "cas", "partial"])
def test_global_memory_failures_do_not_write(monkeypatch, failure):
    audit, memory, writes = memory_audit(monkeypatch, active=failure == "active")
    identity = dict(generation=1, stateEpoch=2, tick=90)
    if failure in identity:
        identity[failure] += 1
    expected, replacement = bytes(STATE_SIZE), perturb_state(bytes(STATE_SIZE))
    if failure == "cas":
        expected = replacement
    if failure == "partial":
        expected = None
    with pytest.raises(ValueError):
        audit.global_state(identity, expected=expected, replacement=replacement)
    assert writes == []


def probe_fixture():
    process = dict(child_pid=17, child_start_ticks="91")
    original = bytes(STATE_SIZE)
    def census(content, tick, epoch):
        return dict(**process, libg_sha256=LIBG_SHA, global_offset=hex(GLOBAL_OFFSET), readonly=True,
            state=state_summary(content), generation=1, stateEpoch=epoch, tick=tick)
    rows = []
    for tick in (90, 1200, 3600, 5400):
        perturbed = perturb_state(original)
        rows.append(dict(threshold=tick, original_hex=original.hex(), perturbed_hex=perturbed.hex(),
            restored_hex=perturbed.hex(), classification="retained_perturbation", before=census(original, tick, 2),
            restored=census(perturbed, tick, 3), repaired=census(original, tick, 3),
            before_digest="a" * 64, perturbed_digest="a" * 64, final_digest="a" * 64,
            snapshot_released=True, no_native_steps_while_perturbed=True,
            stale_rejected=["generation", "stateEpoch", "tick"]))
    report = dict(schema="fair-final-native-global-rng-probe.v1", status="completed_diagnostic", second_process=False,
        behavior="legal-WAIT-with-isolated-global-perturbation-no-step", startup=census(original, 0, 1),
        startup_hex=original.hex(), records=rows, fixed_hog_usage_proven=False,
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False)
    return report, process


def test_diagnostic_accepts_either_bytewise_restore_outcome():
    report, process = probe_fixture()
    validate_probe(report, process, second_process=False)
    for row in report["records"]:
        row["restored_hex"] = row["original_hex"]
        row["restored"]["state"] = row["before"]["state"]
        row["classification"] = "restored_original"
    validate_probe(report, process, second_process=False)


@pytest.mark.parametrize("mutation", ["scope", "classification", "repair", "digest", "steps", "stale", "pid", "stage", "epoch"])
def test_native_diagnostic_rejects_content_tampering(mutation):
    report, process = probe_fixture()
    row = report["records"][0]
    if mutation == "scope":
        report["gates_closed"] = True
    elif mutation == "classification":
        row["classification"] = "restored_original"
    elif mutation == "repair":
        row["repaired"]["state"] = copy.deepcopy(row["restored"]["state"])
    elif mutation == "digest":
        row["final_digest"] = "b" * 64
    elif mutation == "steps":
        row["no_native_steps_while_perturbed"] = False
    elif mutation == "stale":
        row["stale_rejected"].pop()
    elif mutation == "pid":
        row["restored"]["child_pid"] += 1
    elif mutation == "stage":
        report["records"].pop()
    else:
        row["restored"]["stateEpoch"] = row["before"]["stateEpoch"]
    with pytest.raises(ValueError):
        validate_probe(report, process, second_process=False)


def test_static_report_recomputes_on_private_library():
    library = Path(__file__).parent.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not library.exists():
        pytest.skip("private native library omitted from public snapshot")
    recorded = json.loads((Path(__file__).parent / "static-report.json").read_text())
    assert json.loads(json.dumps(static_report(library))) == recorded


def test_wrong_library_rejected_before_native_parse(tmp_path):
    path = tmp_path / "wrong.so"
    path.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="library identity"):
        static_report(path)


def archive_fixture(tmp_path):
    from global_rng_audit import PORT, sha
    from run_global import ROOT, upload_sources
    from seed_audit import PROBE_SHA

    archive = tmp_path / "archive"
    archive.mkdir()
    def write(name, value):
        path = archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        return sha(path)
    sources = upload_sources()
    identities = {name: write(name, content) for name, content in sources.items()}
    settings = dict(port=PORT, probe="/fixture/probe", engine_sha256=PROBE_SHA,
        source_sha256=dict(identities))
    identities["settings.json"] = write("settings.json", settings)
    frozen = {"fixture-runtime.py": "a" * 64}
    for second, variant in enumerate(("immediate", "delayed")):
        report, process = probe_fixture()
        process["child_pid"] += second
        for census in [report["startup"], *[row[name] for row in report["records"]
                for name in ("before", "restored", "repaired")]]:
            census["child_pid"] = process["child_pid"]
        process.update(phase="stopped", phase_snapshot_engine=dict(path=settings["probe"], sha256=PROBE_SHA))
        report.update(second_process=bool(second), source_sha256=frozen)
        write(variant + "/engine/process.json", process)
        write(variant + "/cleanup.json", dict(exit_code=0, error=None, clean=True, listening=False,
            pids_alive=dict(pid=False, child_pid=False), port=PORT, process=process))
        write(variant + "/report.json", report)
        write(variant + "/progress.json", dict(records=report["records"]))
    write("worker.log", b"")
    launch = dict(schema="fair-final-native-global-rng-launch.v1", settings=settings,
        uploaded_sha256=identities, runner_sha256=sha(ROOT / "run_global.py"),
        static_report_sha256=sha(ROOT / "static-report.json"), contract_sha256=sha(ROOT / "CONTRACT.md"),
        frozen_runtime_sha256=frozen, training_admissible=False, gates_closed=False)
    (tmp_path / "launch.json").write_text(json.dumps(launch))
    launch_sha = sha(tmp_path / "launch.json")
    def receipt():
        record = dict(schema="fair-final-native-global-rng-download.v1", launch_sha256=launch_sha,
            files={str(path.relative_to(archive)): sha(path) for path in archive.rglob("*") if path.is_file()},
            source_worker_stopped=True, training_admissible=False, gates_closed=False)
        (tmp_path / "download.json").write_text(json.dumps(record))
        return sha(tmp_path / "download.json")
    return archive, launch_sha, receipt


def test_archive_verifies_two_process_diagnostic_without_upgrading_gate(tmp_path):
    archive, launch_sha, receipt = archive_fixture(tmp_path)
    result = validate(tmp_path, launch_sha, receipt())
    assert result["native_processes"] == 2 and result["tested_boundaries"] == 8
    assert result["classifications"] == ["retained_perturbation"] * 8
    assert not result["gates_closed"] and not result["training_admissible"]


@pytest.mark.parametrize("mutation", ["failure", "cleanup", "reused_process", "runtime", "source", "progress", "missing"])
def test_archive_rejects_semantic_tampering_even_with_rehashed_receipt(tmp_path, mutation):
    archive, launch_sha, receipt = archive_fixture(tmp_path)
    if mutation == "failure":
        (archive / "immediate/failure.json").write_text("{}")
    elif mutation == "source":
        with (archive / "global_rng_audit.py").open("ab") as handle:
            handle.write(b"\nfixture mutation\n")
    elif mutation == "missing":
        (archive / "delayed/report.json").unlink()
    elif mutation == "reused_process":
        for name in ("engine/process.json", "cleanup.json", "report.json", "progress.json"):
            (archive / "delayed" / name).write_bytes((archive / "immediate" / name).read_bytes())
        path = archive / "delayed/report.json"
        content = json.loads(path.read_text())
        content["second_process"] = True
        path.write_text(json.dumps(content))
    else:
        name = "delayed/cleanup.json" if mutation == "cleanup" else "delayed/report.json"
        if mutation == "progress":
            name = "delayed/progress.json"
        path = archive / name
        content = json.loads(path.read_text())
        if mutation == "cleanup":
            content["pids_alive"]["child_pid"] = True
        elif mutation == "runtime":
            content["source_sha256"] = {}
        else:
            content["records"].pop()
        path.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        validate(tmp_path, launch_sha, receipt())


def test_archive_rejects_wrong_external_identity(tmp_path):
    archive, launch_sha, receipt = archive_fixture(tmp_path)
    receipt_sha = receipt()
    for launch_id, receipt_id in (("0" * 64, receipt_sha), (launch_sha, "0" * 64)):
        with pytest.raises(ValueError, match="external identities"):
            validate(tmp_path, launch_id, receipt_id)
