import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path[:0] = [str(Path(__file__).resolve().parent.parent / name) for name in (
    "fair-final-random-execution-v1", "fair-final-history-v2/project", "fair-final-migration-integration-v1")]
from test_plugin import BASE, header, record
from test_trace_verification import trajectory
from plugin_protocol import filter_spec, sha, summarize
from verify_plugin import validate_build_archive, verify


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


@pytest.fixture
def archive_fixture(tmp_path, monkeypatch):
    import run_plugin
    import run_trace
    import verify_plugin
    attempt = tmp_path / "attempt"
    archive = attempt / "archive"
    archive.mkdir(parents=True)
    ancestor, build, parent = (tmp_path / name for name in ("ancestor", "build", "parent"))
    for root in (ancestor, build, parent):
        root.mkdir()
    sources = {"plugin_launcher.py": b"launcher", "random_entries.c": b"plugin"}
    monkeypatch.setattr(run_plugin, "runtime_sources", lambda: sources)
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in sources.items()}
    for name, content in sources.items():
        (archive / name).write_bytes(content)
    settings = dict(root="/remote", port=29787, project="/project", base_project="/base",
        puct_root="/puct", probe="/engine/probe.so", engine_sha256="1" * 64,
        qemu="/qemu-old", qemu_sha256="2" * 64, plugin_qemu="/qemu-new", plugin_qemu_sha256="3" * 64,
        service_sha256="4" * 64, plugin="/plugin.so", plugin_sha256="5" * 64, source_sha256=hashes)
    proof = dict(engine_sha256=settings["engine_sha256"])
    parent_record = dict(settings=settings, frozen_runtime_sha256={"runtime.py": "6" * 64},
        parent_launch_sha256="7" * 64, parent_receipt_sha256="8" * 64, parent_verification=proof)
    write(ancestor / "launch.json", parent_record)
    monkeypatch.setattr(run_trace, "validate_parent", lambda *args: proof)
    build_record = dict(builder_sha256=hashlib.sha256(b"builder").hexdigest())
    write(build / "launch.json", build_record)
    (build / "archive").mkdir()
    (build / "archive/build_runtime.py").write_bytes(b"builder")
    built = dict(qemu_sha256=settings["plugin_qemu_sha256"], plugin_header_sha256="9" * 64)
    monkeypatch.setattr(verify_plugin, "validate_build_archive", lambda *args: built)
    compilation = dict(source_sha256=hashes["random_entries.c"], plugin_sha256=settings["plugin_sha256"],
        header_sha256=built["plugin_header_sha256"])
    write(archive / "compile.json", compilation)
    (archive / "compile.log").write_bytes(b"")
    (archive / "worker.log").write_bytes(b"")
    write(archive / "settings.json", settings)
    write(archive / "completion.json", dict(status="completed_requires_independent_verification",
        settings_sha256=sha(archive / "settings.json"), training_admissible=False, gates_closed=False))
    for index, variant in enumerate(("baseline", "upgraded", "plugin")):
        folder = archive / variant
        configuration = dict(settings, variant=variant, center_base=BASE if variant == "plugin" else None)
        write(folder / "plugin-settings.json", configuration)
        old = variant == "baseline"
        qemu = settings["qemu"] if old else settings["plugin_qemu"]
        argv = [qemu]
        if variant == "plugin":
            window = filter_spec(BASE)
            argv += ["-plugin", f"/plugin.so,first={window['first_base']:x},last={window['last_base']:x},output=/remote/plugin/qemu.log"]
        argv += [settings["probe"], "29787"]
        identity = dict(pid=100 + index, start_ticks=str(200 + index))
        process = dict(**identity, child_pid=300 + index, child_start_ticks=str(400 + index), port=29787,
            phase="stopped", ready=False, argv=argv, random_plugin_trace=dict(variant=variant,
                settings_sha256=sha(folder / "plugin-settings.json"), launcher_sha256=hashes["plugin_launcher.py"],
                qemu_sha256=settings["qemu_sha256"] if old else settings["plugin_qemu_sha256"],
                plugin_sha256=settings["plugin_sha256"] if variant == "plugin" else None),
            phase_snapshot_engine=dict(path=settings["probe"], sha256=settings["engine_sha256"],
                launcher_sha256=hashes["plugin_launcher.py"], service_sha256=settings["service_sha256"]))
        address = dict(readonly=True, child_pid=process["child_pid"], child_start_ticks=process["child_start_ticks"],
            libg_base=BASE, manager=0x10000, tick=90, generation=1, stateEpoch=1)
        before = dict(address, manager=0, tick=-1, generation=0, stateEpoch=0)
        report = dict(trajectory(), before=before, opening=address, final=dict(address, tick=93),
            status="completed_pending_trace_verification", mode="trace", source_sha256=parent_record["frozen_runtime_sha256"],
            engine_sha256=settings["engine_sha256"], seconds=1, match_seed=78102011, execution_seed=78102021,
            training_admissible=False, gates_closed=False)
        write(folder / "address-evidence.json", before)
        write(folder / "engine/process.json", process)
        write(folder / "engine-owner.json", dict(process=identity))
        write(folder / "cleanup.json", dict(clean=True, port_released=True, exit_code=0,
            process=process, launcher=identity))
        write(folder / "probe-process.json", dict(exit_code=0, seconds=1))
        write(folder / "phase-completion.json", dict(variant=variant, seconds=2,
            training_admissible=False, gates_closed=False))
        write(folder / "measurements.json", dict(schema="FairFinalRuntimeMeasurements/v1",
            records=[dict(tick=93, observation_sha256="a" * 64, process_local_digest=dict(tick=93,
                scope="native-payload+global-mt2504.v1", sha256="b" * 64, nativeSha256="c" * 64))],
            training_admissible=False, gates_closed=False))
        if variant == "plugin":
            text = header() + record(0, 0x121B118, BASE + 0x19FC368) + record(1, 0x121B2E4, 0x100A0)
            report.update(reset_offset=len(header()), battle_offset=len(text), final_offset=len(text))
            (folder / "qemu.log").write_text(text + "END\t2\n")
            write(folder / "trace-summary.json", summarize(folder / "qemu.log", base=BASE, center=BASE,
                reset_offset=report["reset_offset"], battle_offset=report["battle_offset"], final_offset=report["final_offset"],
                global_object=BASE + 0x19FC368, manager_object=0x100A0))
        write(folder / "report.json", report)
    launch = dict(schema="FairFinalPluginLaunch/v1", settings=settings, source_sha256=hashes,
        frozen_runtime_sha256=parent_record["frozen_runtime_sha256"], ancestor_launch_sha256=sha(ancestor / "launch.json"),
        build_launch_sha256=sha(build / "launch.json"), builder=build_record, build=built, compile=compilation,
        settings_sha256=sha(archive / "settings.json"), training_admissible=False, gates_closed=False)
    write(attempt / "launch.json", launch)
    return dict(attempt=attempt, ancestor=ancestor, build_attempt=build, snapshot_parent=parent)


def verify_fixture(fixture):
    attempt = fixture["attempt"]
    files = {path.relative_to(attempt / "archive").as_posix(): dict(sha256=sha(path), bytes=path.stat().st_size)
        for path in (attempt / "archive").rglob("*") if path.is_file()}
    write(attempt / "download.json", dict(schema="FairFinalPluginDownload/v1", files=files,
        launch_sha256=sha(attempt / "launch.json"), worker_stopped=True, training_admissible=False, gates_closed=False))
    return verify(**fixture, launch_sha=sha(attempt / "launch.json"), receipt_sha=sha(attempt / "download.json"))


def test_synthetic_complete_archive_is_independently_recomputed(archive_fixture):
    result = verify_fixture(archive_fixture)
    assert result["status"] == "verified_limited_scope"
    assert result["comparison"]["compared_variants"] == 3
    assert result["captured_entries"] == 2
    assert result["training_admissible"] is result["hidden_random_coverage_closed"] is False


@pytest.mark.parametrize("change", ["cleanup", "same_process", "wrong_qemu", "source", "terminal", "mask", "summary", "footer", "header", "extra", "missing", "scope", "configuration", "parent"])
def test_recomputed_receipt_still_rejects_semantic_tampering(archive_fixture, change):
    archive = archive_fixture["attempt"] / "archive"
    if change == "source":
        (archive / "random_entries.c").write_bytes(b"changed")
    elif change == "footer":
        path = archive / "plugin/qemu.log"
        path.write_text(path.read_text().replace("END\t2", "END\t1"))
    elif change == "header":
        path = archive / "plugin/qemu.log"
        path.write_text(path.read_text().replace("/v1", "/v2"))
    elif change == "extra":
        (archive / "failure.json").write_text("{}")
    elif change == "missing":
        (archive / "plugin/measurements.json").unlink()
    elif change == "parent":
        (archive_fixture["ancestor"] / "launch.json").write_text("{}")
    else:
        name = {"cleanup": "plugin/cleanup.json", "same_process": "plugin/engine/process.json",
            "wrong_qemu": "upgraded/engine/process.json", "terminal": "plugin/report.json",
            "mask": "plugin/report.json", "summary": "plugin/trace-summary.json",
            "scope": "completion.json", "configuration": "plugin/plugin-settings.json"}[change]
        path = archive / name
        value = json.loads(path.read_text())
        if change == "cleanup": value["clean"] = False
        elif change == "same_process": value["child_pid"] = 300
        elif change == "wrong_qemu": value["argv"][0] = "/other"
        elif change == "terminal": value["terminal"]["finalized"] = False
        elif change == "mask": value["decisions"][0]["choices"]["0"]["action"]["delay_slot"] = 2
        elif change == "summary": value["captured_entries"] += 1
        elif change == "scope": value["training_admissible"] = True
        else: value["center_base"] += 4096
        write(path, value)
    with pytest.raises(ValueError):
        verify_fixture(archive_fixture)


def test_real_successful_runtime_build_archive_is_recomputed():
    root = Path(__file__).resolve().parent
    attempt = root / "build-1792746865344999000"
    if not attempt.exists():
        pytest.skip("private runtime build artifacts excluded from public snapshot")
    result = validate_build_archive(attempt, "cf3893712eb9a69f5c13cb23a809af1299a83e34b24a1c90651a3e43fd256796")
    assert result["qemu_sha256"] == "a7fcd2082e93b8bfd9aefef7906f4227a33d7bd230084d5fae29dbe33536a62f"


@pytest.mark.parametrize("attempt_name,launch_sha,receipt_sha", [
    ("native-plugin-1792747111942680000", "0444e53f1ae35f9c250c967fba74c3b9e0209f6181e9dc76bf9d2585ef9a5bd6", "12db403f6e78509098be31163ba1e5e5399bde7e4f752b209938a8ad137c4c5d"),
    ("native-plugin-1792747984532934000", "93ea656bbf0af30fca4b9fe7f0d2ef9d28cde56acfb7de27b31899126f1a44da", "05a27608b32f872f09da7ca935c79f5e87a66a81d5a67e6e8dbabe8ff6d8f6f8"),
    ("native-plugin-1792748668033706000", "250450d66769dcbd7f0f123e704003768c29db3f787c7b4d6d3badc48d4095f6", "04892c4a82d9cb275450b441b92bd46488d6144031076bd3b8b06f6303871809"),
    ("native-plugin-1792749088144663000", "3102ca6c266b81fa82749e1d1c25b584ff94e3c4db6a9774e69bce5f723dbac0", "d022cb074b2a73c5eb67bc16cc2b372e6700e28f3d124987ddc2ad446675f854"),
    ("native-plugin-1792749986852677000", "e80503fca70a8a4e7ba509960945533864a027587d57998f776a011ad37bf15e", "01fb129d1da6efd4fd0a1ed3bf386bea41357d192bfb654d50e40a4a552f308d"),
])
def test_actual_failed_native_archives_are_rejected(attempt_name, launch_sha, receipt_sha):
    root = Path(__file__).resolve().parent
    attempt = root / attempt_name
    if not attempt.exists():
        pytest.skip("private native plugin attempts excluded from public snapshot")
    with pytest.raises(ValueError, match="missing evidence or archived failure"):
        verify(attempt, launch_sha, receipt_sha,
            ancestor=root.parent / "fair-final-random-execution-v1/native-execution-1792744646333119000",
            build_attempt=root / "build-1792746865344999000",
            snapshot_parent=root.parent / "fair-final-global-rng-snapshot-v1/native-snapshot-1792734107610915000")


def test_actual_complete_native_plugin_archive_is_independently_recomputed():
    root = Path(__file__).resolve().parent
    attempt = root / "native-plugin-1792750885374957000"
    if not attempt.exists():
        pytest.skip("private complete native plugin artifacts excluded from public snapshot")
    result = verify(attempt,
        "f57ea71de3911e3992c6998c5ceb7881abe203c396a1be76ce8fd8bb8e4bc3c5",
        "aa9275566bf5a73df0cc802fd3fc5a43258aa85017b344200fca93048ec2339f",
        ancestor=root.parent / "fair-final-random-execution-v1/native-execution-1792744646333119000",
        build_attempt=root / "build-1792746865344999000",
        snapshot_parent=root.parent / "fair-final-global-rng-snapshot-v1/native-snapshot-1792734107610915000")
    assert result["status"] == "verified_limited_scope"
    assert result["comparison"]["compared_variants"] == 3
    assert result["comparison"]["decisions"] == 426
    assert result["captured_entries"] == 44
    assert result["trace_closure"] == "supervised_after_native_terminal"
    assert result["training_admissible"] is result["hidden_random_coverage_closed"] is False


def supervised_fixture(fixture):
    from plugin_seal import seal_identity
    attempt, archive = fixture["attempt"], fixture["attempt"] / "archive"
    launch = json.loads((attempt / "launch.json").read_text())
    launch["source_sha256"]["plugin_seal.py"] = hashlib.sha256(b"seal-fixture").hexdigest()
    (archive / "plugin_seal.py").write_bytes(b"seal-fixture")
    settings = json.loads((archive / "settings.json").read_text())
    settings["source_sha256"] = launch["source_sha256"]
    write(archive / "settings.json", settings)
    launch["settings"], launch["settings_sha256"] = settings, sha(archive / "settings.json")
    write(attempt / "launch.json", launch)
    completion = json.loads((archive / "completion.json").read_text())
    completion["settings_sha256"] = launch["settings_sha256"]
    write(archive / "completion.json", completion)
    for variant in ("baseline", "upgraded", "plugin"):
        folder = archive / variant
        value = dict(settings, variant=variant, center_base=BASE if variant == "plugin" else None)
        write(folder / "plugin-settings.json", value)
        process = json.loads((folder / "engine/process.json").read_text())
        process["random_plugin_trace"]["settings_sha256"] = sha(folder / "plugin-settings.json")
        write(folder / "engine/process.json", process)
        cleanup = json.loads((folder / "cleanup.json").read_text())
        cleanup["process"] = process
        write(folder / "cleanup.json", cleanup)
    folder = archive / "plugin"
    content = header().replace("/v1", "/v2")
    for index, (offset, obj) in enumerate(((0x121B118, BASE + 0x19FC368), (0x121B2E4, 0x100A0))):
        fields = record(index, offset, obj).rstrip().split("\t")
        fields.insert(4, f"{BASE + offset - 4:016x}")
        content += "\t".join(fields) + "\n"
    (folder / "qemu.log").write_text(content)
    report = json.loads((folder / "report.json").read_text())
    report.update(reset_offset=len(header()), battle_offset=len(content), final_offset=len(content))
    write(folder / "report.json", report)
    seal = seal_identity(folder, report, json.loads((folder / "cleanup.json").read_text()))
    write(folder / "trace-seal.json", seal)
    write(folder / "trace-summary.json", summarize(folder / "qemu.log", base=BASE, center=BASE,
        reset_offset=report["reset_offset"], battle_offset=report["battle_offset"], final_offset=report["final_offset"],
        global_object=BASE + 0x19FC368, manager_object=0x100A0, seal=seal))
    return folder


def test_supervised_eof_requires_native_completion_and_bound_raw_bytes(archive_fixture, monkeypatch):
    import run_plugin
    folder = supervised_fixture(archive_fixture)
    monkeypatch.setattr(run_plugin, "runtime_sources", lambda: {name: b"" for name in
        json.loads((archive_fixture["attempt"] / "launch.json").read_text())["source_sha256"]})
    result = verify_fixture(archive_fixture)
    assert result["captured_entries"] == 2
    from plugin_protocol import parse
    with pytest.raises(ValueError, match="footer absent"):
        list(parse(folder / "qemu.log", base=BASE, center=BASE))


@pytest.mark.parametrize("change", ["raw", "partial", "error", "sequence", "seal", "final_offset", "cleanup", "terminal", "live"])
def test_supervised_seal_rejects_incomplete_or_unowned_evidence(archive_fixture, monkeypatch, change):
    import run_plugin
    import selected_collection_worker
    from plugin_seal import create_seal
    folder = supervised_fixture(archive_fixture)
    monkeypatch.setattr(run_plugin, "runtime_sources", lambda: {name: b"" for name in
        json.loads((archive_fixture["attempt"] / "launch.json").read_text())["source_sha256"]})
    if change == "live":
        monkeypatch.setattr(selected_collection_worker, "matching_process", lambda *args: True)
        with pytest.raises(ValueError, match="still alive"):
            create_seal(folder)
        return
    if change in ("raw", "partial", "error", "sequence"):
        path = folder / "qemu.log"
        text = path.read_text()
        if change == "raw": text = text.replace("000000000000004d", "000000000000004e")
        elif change == "partial": text = text[:-1]
        elif change == "error": text += "ERROR\tfailed\n"
        else: text = text.replace("E\t1\t", "E\t2\t")
        path.write_text(text)
        if change != "raw":
            value = json.loads((folder / "trace-seal.json").read_text())
            value.update(trace_sha256=sha(path), trace_bytes=path.stat().st_size)
            write(folder / "trace-seal.json", value)
    else:
        name = {"seal": "trace-seal.json", "final_offset": "trace-seal.json", "cleanup": "cleanup.json", "terminal": "report.json"}[change]
        value = json.loads((folder / name).read_text())
        if change == "seal": value["child_start_ticks"] = "other"
        elif change == "final_offset": value["final_offset"] -= 1
        elif change == "cleanup": value["clean"] = False
        else: value["terminal"]["finalized"] = False
        write(folder / name, value)
    with pytest.raises(ValueError):
        verify_fixture(archive_fixture)


def test_v3_archive_binds_arm_to_its_own_process_not_previous_process(archive_fixture, monkeypatch):
    import run_plugin
    from plugin_seal import seal_identity
    attempt = archive_fixture["attempt"]
    archive = attempt / "archive"
    folder = supervised_fixture(archive_fixture)
    launch = json.loads((attempt / "launch.json").read_text())
    settings = launch["settings"]
    settings["arm_after_ready"] = True
    write(archive / "settings.json", settings)
    launch["settings_sha256"] = sha(archive / "settings.json")
    write(attempt / "launch.json", launch)
    completion = json.loads((archive / "completion.json").read_text())
    completion["settings_sha256"] = launch["settings_sha256"]
    write(archive / "completion.json", completion)
    for variant in ("baseline", "upgraded", "plugin"):
        target = archive / variant
        write(target / "plugin-settings.json", dict(settings, variant=variant, center_base=None))
        process = json.loads((target / "engine/process.json").read_text())
        process["random_plugin_trace"]["settings_sha256"] = sha(target / "plugin-settings.json")
        if variant == "plugin":
            process["argv"][2] = "/plugin.so,arm=/remote/plugin/arm-base.txt,output=/remote/plugin/qemu.log"
        write(target / "engine/process.json", process)
        cleanup = json.loads((target / "cleanup.json").read_text())
        cleanup["process"] = process
        write(target / "cleanup.json", cleanup)
    text = (folder / "qemu.log").read_text().split("\n", 1)[1]
    text = "FairFinalPluginEntries/v3\n" + f"ARM\t{BASE:016x}\n" + text
    (folder / "qemu.log").write_text(text)
    (folder / "arm-base.txt").write_text(f"{BASE:016x}\n")
    report = json.loads((folder / "report.json").read_text())
    report.update(reset_offset=len("FairFinalPluginEntries/v3\n"), battle_offset=len(text), final_offset=len(text))
    write(folder / "report.json", report)
    seal = seal_identity(folder, report, json.loads((folder / "cleanup.json").read_text()))
    write(folder / "trace-seal.json", seal)
    write(folder / "trace-summary.json", summarize(folder / "qemu.log", base=BASE, center=BASE,
        reset_offset=report["reset_offset"], battle_offset=report["battle_offset"], final_offset=report["final_offset"],
        global_object=BASE + 0x19FC368, manager_object=0x100A0, seal=seal))
    monkeypatch.setattr(run_plugin, "runtime_sources", lambda: {name: b"" for name in launch["source_sha256"]})
    assert verify_fixture(archive_fixture)["captured_entries"] == 2
    (folder / "arm-base.txt").write_text(f"{BASE+4096:016x}\n")
    with pytest.raises(ValueError, match="arming record differs"):
        verify_fixture(archive_fixture)
