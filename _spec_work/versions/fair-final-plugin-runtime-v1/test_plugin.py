import io
from copy import deepcopy
import os
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path[:0] = [str(Path(__file__).resolve().parent.parent / name) for name in (
    "fair-final-global-rng-snapshot-v1", "fair-final-native-seed-audit-v1",
    "fair-final-native-global-rng-audit-v1", "fair-final-history-v2/project")]
from build_runtime import run, source_filter
from plugin_protocol import parse, summarize
from trace_protocol import TARGETS, filter_spec


BASE = 0x4002B2C00000


def header():
    window = filter_spec(BASE)
    return f"FairFinalPluginEntries/v1\t{window['first_base']:016x}\t{window['last_base']:016x}\n"


def record(sequence, offset, obj=0x1234, *, cpu=0, base=BASE):
    return f"E\t{sequence}\t{cpu}\t{base + offset:016x}\t{obj:016x}\t{77:016x}\t{base + 0x12000:016x}\n"


def write(tmp_path, content):
    path = tmp_path / "entries.tsv"
    path.write_text(content)
    return path


def test_exact_entry_registers_and_footer(tmp_path):
    path = write(tmp_path, header() + record(0, 0x121B118) + "END\t1\n")
    entries = list(parse(path, base=BASE, center=BASE))
    assert len(entries) == 1
    assert entries[0]["function"] == "mt_initialize"
    assert entries[0]["object"] == 0x1234
    assert entries[0]["argument"] == 77
    assert entries[0]["caller_offset"] == 0x11FFC
    assert path.read_bytes()[entries[0]["file_offset"]:entries[0]["end_offset"]].startswith(b"E\t0\t0\t")


def test_all_targets_and_candidate_base_filter(tmp_path):
    text = header()
    for sequence, offset in enumerate(TARGETS):
        text += record(sequence, offset, cpu=sequence % 2)
    text += record(8, 0x121B188, base=BASE + 4096) + "END\t9\n"
    entries = list(parse(write(tmp_path, text), base=BASE, center=BASE))
    assert [entry["function"] for entry in entries] == list(TARGETS.values())


def test_v2_instruction_callback_identity_preserves_distinct_sampled_pc(tmp_path):
    fields = record(0, 0x121B118).rstrip().split("\t")
    fields.insert(4, f"{BASE + 0x121B114:016x}")
    content = header().replace("/v1", "/v2") + "\t".join(fields) + "\nEND\t1\n"
    entries = list(parse(write(tmp_path, content), base=BASE, center=BASE))
    assert entries[0]["pc"] == BASE + 0x121B118
    assert entries[0]["sampled_pc"] == BASE + 0x121B114
    assert entries[0]["object"] == 0x1234
    assert entries[0]["argument"] == 77


def test_v3_requires_exact_actual_process_arm_before_any_entry(tmp_path):
    fields = record(0, 0x121B118).rstrip().split("\t")
    fields.insert(4, f"{BASE + 0x121B114:016x}")
    entry = "\t".join(fields) + "\n"
    prefix = "FairFinalPluginEntries/v3\n"
    good = prefix + f"ARM\t{BASE:016x}\n" + entry + "END\t1\n"
    assert len(list(parse(write(tmp_path, good), base=BASE, center=BASE))) == 1
    for bad in (prefix + entry + "END\t1\n", good.replace(f"ARM\t{BASE:016x}", f"ARM\t{BASE+4096:016x}"),
            good.replace(entry, record(0, 0x121B118)), good + "ARM\tlate\n"):
        with pytest.raises(ValueError):
            list(parse(write(tmp_path, bad), base=BASE, center=BASE))


@pytest.mark.parametrize("bad", [
    "", "ERROR\tshort_read\n", "END\t1\n", "END\t0\ntrailing\n",
    record(1, 0x121B188) + "END\t1\n",
    record(0, 0x121B188).replace("E\t0\t0", "E\t0\t00") + "END\t1\n",
    record(0, 0x121B188, cpu=4096) + "END\t1\n",
    record(0, 0x121B189) + "END\t1\n",
    record(0, 0x121B188, base=BASE + 128 * 4096) + "END\t1\n",
    record(0, 0x121B188).replace("000000000000004d", "4d") + "END\t1\n",
    record(0, 0x121B188) + "END\t0\n",
    record(0, 0x121B188) + "END\t1",
])
def test_reject_incomplete_or_forged_records(tmp_path, bad):
    with pytest.raises(ValueError):
        list(parse(write(tmp_path, header() + bad), base=BASE, center=BASE))


def test_reject_header_drift(tmp_path):
    with pytest.raises(ValueError, match="header"):
        list(parse(write(tmp_path, header().replace("/v1", "/v3") + "END\t0\n"), base=BASE, center=BASE))


def test_phase_positive_controls_and_objects(tmp_path):
    startup = header() + record(0, 0x121B188)
    reset = startup + record(1, 0x121B118, BASE + 0x19FC368) + record(2, 0x121B2E4, 0x100A0)
    final = reset + record(3, 0x121B350, 0x100A0)
    path = write(tmp_path, final + "END\t4\n")
    result = summarize(path, base=BASE, center=BASE, reset_offset=len(startup),
        battle_offset=len(reset), final_offset=len(final), global_object=BASE + 0x19FC368,
        manager_object=0x100A0)
    assert result["phase_counts"]["reset"] == {"mt_initialize": 1, "xorshift_seed": 1}
    assert result["phase_counts"]["battle"] == {"xorshift_generate": 1}
    assert [entry["object_kind"] for entry in result["records"]] == ["other", "global_mt", "manager", "manager"]
    assert result["hidden_random_coverage_closed"] is False
    assert result["training_admissible"] is False


def test_absent_positive_controls_are_not_nonreachability(tmp_path):
    text = header() + record(0, 0x121B188)
    path = write(tmp_path, text + "END\t1\n")
    with pytest.raises(ValueError, match="positive controls"):
        summarize(path, base=BASE, center=BASE, reset_offset=len(header()),
            battle_offset=len(text), final_offset=len(text), global_object=1, manager_object=2)


@pytest.mark.parametrize("boundary", ["reset_offset", "battle_offset", "final_offset"])
def test_phase_must_not_split_a_register_record(tmp_path, boundary):
    content = header() + record(0, 0x121B118) + record(1, 0x121B2E4)
    path = write(tmp_path, content + "END\t2\n")
    positions = dict(reset_offset=len(header()), battle_offset=len(content), final_offset=len(content))
    if boundary == "reset_offset":
        positions[boundary] += 1
    else:
        positions[boundary] -= 1
        if boundary == "final_offset":
            positions["battle_offset"] = positions[boundary]
    with pytest.raises(ValueError, match="splits"):
        summarize(path, base=BASE, center=BASE, global_object=1, manager_object=2, **positions)


def test_source_filter_omits_firmware_and_preserves_path_checks(tmp_path):
    member = tarfile.TarInfo("qemu-10.0.13/roms/edk2/absolute")
    member.type = tarfile.SYMTYPE
    member.linkname = "/usr/include/X11"
    assert source_filter(member, str(tmp_path)) is None
    member.name = "qemu-10.0.13/linux-user/absolute"
    with pytest.raises(tarfile.AbsoluteLinkError):
        source_filter(member, str(tmp_path))


def test_source_filter_extracts_regular_build_input(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo("qemu-10.0.13/configure")
        member.size = 4
        archive.addfile(member, io.BytesIO(b"test"))
    stream.seek(0)
    with tarfile.open(fileobj=stream) as archive:
        archive.extractall(tmp_path, filter=source_filter)
    assert (tmp_path / "qemu-10.0.13/configure").read_bytes() == b"test"


def test_failed_builder_step_retains_log_and_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("build_runtime.resources", lambda root: {})
    monkeypatch.setattr("build_runtime.process_start_ticks", lambda pid: "test-fixture")
    with pytest.raises(RuntimeError, match="exited 7"):
        run(tmp_path, "failed", [sys.executable, "-c", "print('diagnostic'); raise SystemExit(7)"], dict(os.environ))
    assert "diagnostic" in (tmp_path / "failed.log").read_text()
    assert (tmp_path / "status.json").is_file()


def test_identity_recording_error_still_terminates_child(tmp_path, monkeypatch):
    monkeypatch.setattr("build_runtime.resources", lambda root: {})
    def fail(pid):
        raise ValueError("injected identity failure")
    monkeypatch.setattr("build_runtime.process_start_ticks", fail)
    with pytest.raises(ValueError, match="identity failure"):
        run(tmp_path, "identity", [sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ))


def test_runtime_arguments_pin_variant_and_binary_bytes(tmp_path):
    from plugin_launcher import plugin_arguments
    from plugin_protocol import sha
    qemu = tmp_path / "qemu"
    qemu.write_bytes(b"qemu-fixture")
    plugin = tmp_path / "entries.so"
    plugin.write_bytes(b"plugin-fixture")
    settings = dict(variant="baseline", plugin_qemu=str(qemu), plugin_qemu_sha256=sha(qemu),
        plugin=str(plugin), plugin_sha256=sha(plugin), center_base=BASE)
    assert plugin_arguments(["/old-qemu", "guest"], settings, tmp_path / "log") == ["/old-qemu", "guest"]
    settings["variant"] = "upgraded"
    assert plugin_arguments(["/old-qemu", "guest"], settings, tmp_path / "log") == [str(qemu), "guest"]
    settings["variant"] = "plugin"
    arguments = plugin_arguments(["/old-qemu", "guest"], settings, tmp_path / "log")
    assert arguments[:2] == [str(qemu), "-plugin"]
    assert "first=" in arguments[2] and "last=" in arguments[2]
    assert arguments[-1] == "guest"
    settings["reserved_va"] = 1024**4
    arguments = plugin_arguments(["/old-qemu", "guest"], settings, tmp_path / "reserved-log")
    assert arguments[arguments.index("-R") + 1] == hex(1024**4)
    settings["reserved_va"] = 4096
    with pytest.raises(ValueError, match="reserved"):
        plugin_arguments(["/old-qemu", "guest"], settings, tmp_path / "reserved-log")
    del settings["reserved_va"]
    settings["arm_after_ready"] = True
    arguments = plugin_arguments(["/old-qemu", "guest"], settings, tmp_path / "log")
    assert f"arm={tmp_path}/arm-base.txt" in arguments[2]
    assert "first=" not in arguments[2]
    plugin.write_bytes(b"different")
    with pytest.raises(ValueError, match="configuration"):
        plugin_arguments(["/old-qemu", "guest"], settings, tmp_path / "log")
    settings["variant"] = "upgraded"
    qemu.write_bytes(b"different")
    with pytest.raises(ValueError, match="QEMU"):
        plugin_arguments(["/old-qemu", "guest"], settings, tmp_path / "log")


def comparison_fixture():
    from verify_plugin import comparable
    report = dict(decisions=[dict(tick=90, choices={"0": {"fair_sha": "a" * 64}})],
        outcomes=[], executed=0, steps=510, terminal={"tick": 600, "winner": 0},
        policy_closed_tick=599, final_clock={}, match_seed=1, execution_seed=2,
        source_sha256={}, engine_sha256="c" * 64, final={"tick": 600})
    measurements = dict(schema="FairFinalRuntimeMeasurements/v1", training_admissible=False,
        gates_closed=False, records=[dict(tick=tick, observation_sha256="b" * 64,
            process_local_digest=dict(tick=tick, scope="native-payload+global-mt2504.v1",
                sha256="d" * 64, nativeSha256="e" * 64)) for tick in (300, 600)])
    assert "final" not in comparable(report)
    return [deepcopy(report) for _ in range(3)], [deepcopy(measurements) for _ in range(3)]


def test_runtime_semantic_comparison_ignores_process_local_digest_values():
    from verify_plugin import compare_reports
    reports, measurements = comparison_fixture()
    measurements[1]["records"][0]["process_local_digest"]["sha256"] = "f" * 64
    result = compare_reports(reports, measurements)
    assert result["compared_variants"] == 3
    assert result["cross_process_native_payload_equality_claimed"] is False


@pytest.mark.parametrize("change", ["action", "fair", "terminal", "observation", "coverage", "scope", "digest", "flags"])
def test_runtime_semantic_drift_or_incomplete_measurement_rejected(change):
    from verify_plugin import compare_reports
    reports, measurements = comparison_fixture()
    if change == "action":
        reports[1]["decisions"][0]["choices"]["0"]["action"] = "different"
    elif change == "fair":
        reports[1]["decisions"][0]["choices"]["0"]["fair_sha"] = "f" * 64
    elif change == "terminal":
        reports[1]["terminal"]["winner"] = 1
    elif change == "observation":
        measurements[1]["records"][0]["observation_sha256"] = "f" * 64
    elif change == "coverage":
        measurements[1]["records"].pop()
    elif change == "scope":
        measurements[1]["records"][0]["process_local_digest"]["scope"] = "old"
    elif change == "digest":
        measurements[1]["records"][0]["process_local_digest"]["sha256"] = "d" * 63
    else:
        measurements[1]["training_admissible"] = True
    with pytest.raises(ValueError):
        compare_reports(reports, measurements)


@pytest.fixture
def audit_fixture(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    import plugin_audit
    instance = tmp_path / "instance"
    instance.mkdir()
    process = tmp_path / "process"
    process.mkdir()
    executable = process / "exe"
    executable.write_bytes(b"qemu-test-fixture")
    (process / "cwd").symlink_to(instance, target_is_directory=True)
    (process / "cmdline").write_bytes(b"/isolated/qemu-aarch64\0guest\0")
    (process / "stat").write_text("123 (qemu) " + " ".join(["R"] + ["0"] * 18 + ["456"]))
    configuration = dict(variant="upgraded", plugin_qemu="/isolated/qemu-aarch64",
        plugin_qemu_sha256=plugin_audit.sha(executable))
    settings_path = tmp_path / "plugin-settings.json"
    settings_path.write_text(json.dumps(configuration))
    record = dict(phase="ready", port=29787, child_start_ticks="456", random_plugin_trace=dict(
        qemu_sha256=configuration["plugin_qemu_sha256"], settings_sha256=plugin_audit.sha(settings_path)))
    (instance / "process.json").write_text(json.dumps(record))
    audit = plugin_audit.PluginAudit.__new__(plugin_audit.PluginAudit)
    audit.instance, audit.record, audit.pid, audit.port = instance, deepcopy(record), 123, 29787
    original = Path
    monkeypatch.setattr(plugin_audit, "Path", lambda *parts: tmp_path if parts == ("/proc",) else original(*parts))
    (tmp_path / "123").symlink_to(process, target_is_directory=True)
    return SimpleNamespace(audit=audit, process=process, settings_path=settings_path, record=record, instance=instance)


def test_native_audit_accepts_pinned_executable_instead_of_basename(audit_fixture):
    assert audit_fixture.audit._verify_process().resolve() == audit_fixture.process


@pytest.mark.parametrize("change", ["pid", "command", "executable", "configuration", "port", "process_record", "cwd"])
def test_native_audit_rejects_changed_ownership_or_executable(audit_fixture, tmp_path, change):
    import json
    if change == "pid":
        (audit_fixture.process / "stat").write_text("123 (qemu) " + " ".join(["R"] + ["0"] * 18 + ["457"]))
    elif change == "command":
        (audit_fixture.process / "cmdline").write_bytes(b"/other/qemu-aarch64\0guest\0")
    elif change == "executable":
        (audit_fixture.process / "exe").write_bytes(b"different")
    elif change == "configuration":
        audit_fixture.settings_path.write_text(audit_fixture.settings_path.read_text() + "\n")
    elif change == "port":
        audit_fixture.audit.port += 1
    elif change == "process_record":
        (audit_fixture.instance / "process.json").write_text(json.dumps(dict(audit_fixture.record, phase="exited")))
    else:
        (audit_fixture.process / "cwd").unlink()
        (audit_fixture.process / "cwd").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="ownership"):
        audit_fixture.audit._verify_process()


def test_synthetic_guest_instruction_and_elf_layout():
    import struct
    from plugin_smoke import BASE, ENTRY, FIRST, SECOND, guest_bytes
    content = guest_bytes()
    assert content[:7] == b"\x7fELF\x02\x01\x01"
    assert struct.unpack_from("<H", content, 18)[0] == 183
    assert struct.unpack_from("<Q", content, 24)[0] == BASE + ENTRY
    assert struct.unpack_from("<I", content, FIRST)[0] == 0xD65F03C0
    assert struct.unpack_from("<I", content, SECOND)[0] == 0xD65F03C0
    for address, target in ((ENTRY + 8, FIRST), (ENTRY + 12, SECOND)):
        instruction = struct.unpack_from("<I", content, address)[0]
        assert instruction >> 26 == 0b100101
        assert address + (instruction & ((1 << 26) - 1)) * 4 == target
