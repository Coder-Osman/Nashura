from pathlib import Path

import pytest

from trace_launcher import bound_output_files, command_with_trace, service_source
from trace_protocol import PAGE, TARGETS, check_base, filter_spec, parse_cpu_log, sha, summarize


BASE = 0x4002B2A00000


def dump(offset, *, object=0x1234, caller=0xF3F498, argument=7):
    registers = {number: 0 for number in range(31)}
    registers.update({0: object, 1: argument, 30: BASE + caller + 4})
    lines = [f"Trace 0: 0xaaa [00000000/{BASE + offset:016x}/00000000/00000000] "]
    for first in range(0, 31, 3):
        prefix = f" PC={BASE + offset:016x} " if first == 0 else " "
        lines.append(prefix + " ".join(f"X{number:02d}={registers[number]:016x}"
            for number in range(first, min(first + 3, 31))))
    lines.append("PSTATE=60000000 -ZC- EL0t  SVCR=00000000 --  BTYPE=0")
    return ("\n".join(lines) + "\n").encode()


def test_filter_covers_exact_targets_for_each_registered_base():
    window = filter_spec(BASE)
    assert len(window["expression"].encode()) < 100000
    ranges = [tuple(int(value, 0) for value in item.split("..")) for item in window["expression"].split(",")]
    assert len(ranges) == 128
    for base in (window["first_base"], BASE, window["last_base"]):
        check_base(BASE, base)
        assert all(any(start <= base + target <= end for start, end in ranges) for target in TARGETS)
    with pytest.raises(ValueError, match="escaped"):
        check_base(BASE, window["last_base"] + PAGE)


@pytest.mark.parametrize("base", [True, 0, -PAGE, BASE + 1, 2**63])
def test_invalid_window_base_rejected(base):
    with pytest.raises(ValueError):
        filter_spec(base)


def test_register_parser_preserves_objects_callers_offsets_and_ignores_extra_candidates(tmp_path):
    path = tmp_path / "qemu.log"
    first = dump(0x121B118)
    path.write_bytes(first + dump(0x121B350, object=55, caller=0x1062A20) + dump(0x555))
    rows = list(parse_cpu_log(path, base=BASE))
    assert len(rows) == 2
    assert rows[0]["function"] == "mt_initialize" and rows[0]["object"] == 0x1234
    assert rows[0]["caller_offset"] == 0xF3F498 and rows[0]["argument"] == 7
    assert rows[1]["file_offset"] == len(first) + first.index(b" PC=")
    assert rows[1]["object"] == 55


@pytest.mark.parametrize("damage", ["truncate", "missing_register", "duplicate", "nonascii", "new_pc"])
def test_bad_register_logs_rejected(tmp_path, damage):
    content = dump(0x121B188)
    if damage == "truncate":
        content = content[:content.index(b"PSTATE=")]
    elif damage == "missing_register":
        content = content.replace(b"X02=0000000000000000", b"")
    elif damage == "duplicate":
        content = content.replace(b"X02=0000000000000000", b"X01=0000000000000007")
    elif damage == "new_pc":
        content = content[:content.index(b"PSTATE=")] + dump(0x121B118)
    else:
        content += b"\xff\n"
    path = tmp_path / "bad.log"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        list(parse_cpu_log(path, base=BASE))


def test_summary_checks_positive_controls_phases_and_object_classification(tmp_path):
    startup = dump(0x121B118, object=88)
    reset = dump(0x121B2E4, object=99) + dump(0x121B118)
    battle = dump(0x121B350, object=99, caller=0x1062A20)
    path = tmp_path / "qemu.log"
    path.write_bytes(startup + reset + battle)
    result = summarize(path, base=BASE, reset_offset=len(startup),
        battle_offset=len(startup + reset), final_offset=path.stat().st_size,
        global_object=88, manager_object=99)
    assert result["phase_counts"]["reset"] == {"mt_initialize": 1, "xorshift_seed": 1}
    assert result["phase_counts"]["battle"] == {"xorshift_generate": 1}
    assert [row["object_kind"] for row in result["records"]] == ["global_mt", "manager", "other", "manager"]
    assert result["hidden_random_coverage_closed"] is result["training_admissible"] is False
    with pytest.raises(ValueError, match="positive controls"):
        summarize(path, base=BASE, reset_offset=0, battle_offset=0,
            final_offset=path.stat().st_size, global_object=88, manager_object=99)


@pytest.mark.parametrize("change", ["negative", "order", "beyond", "same_object"])
def test_invalid_trace_phase_identity_rejected(tmp_path, change):
    path = tmp_path / "qemu.log"
    path.write_bytes(b"")
    values = dict(base=BASE, reset_offset=0, battle_offset=0, final_offset=0,
        global_object=88, manager_object=99)
    if change == "negative": values["reset_offset"] = -1
    elif change == "order": values["reset_offset"] = 1
    elif change == "beyond": values["final_offset"] = 1
    else: values["manager_object"] = 88
    with pytest.raises(ValueError, match="phase or random object"):
        summarize(path, **values)


@pytest.mark.parametrize("change", ["empty", "wrong_base", "wrong_reset", "missing_seed", "missing_mt"])
def test_missing_or_displaced_native_positive_controls_rejected(tmp_path, change):
    content = dump(0x121B2E4) + dump(0x121B118) + dump(0x121B350)
    if change == "empty": content = b""
    elif change == "missing_seed": content = dump(0x121B118)
    elif change == "missing_mt": content = dump(0x121B2E4)
    path = tmp_path / "qemu.log"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="positive controls"):
        summarize(path, base=BASE + PAGE if change == "wrong_base" else BASE,
            reset_offset=0, battle_offset=0 if change == "wrong_reset" else len(content),
            final_offset=len(content), global_object=88, manager_object=99)


def test_command_retains_engine_and_adds_complete_execution_logging(tmp_path):
    qemu, probe = tmp_path / "qemu", tmp_path / "probe"
    qemu.write_bytes(b"fixed-qemu"); probe.write_bytes(b"fixed-probe")
    arguments = [str(qemu), "-L", "sysroot", "-E", "LD_LIBRARY_PATH=old", "baseline"]
    settings = dict(qemu_sha256=sha(qemu), mode="trace", center_base=BASE)
    actual = command_with_trace(arguments, probe=probe, engine_sha=sha(probe), baseline="baseline",
        settings=settings, output=tmp_path / "qemu.log")
    assert arguments[-1] == "baseline"
    assert actual[actual.index("-d") + 1] == "exec,cpu,nochain"
    assert actual[actual.index("-dfilter") + 1] == filter_spec(BASE)["expression"]
    assert actual[-1] == str(probe.resolve())
    assert "LD_LIBRARY_PATH=" + str(tmp_path) + ":old" in actual
    settings["mode"] = "calibration"
    calibrated = command_with_trace(arguments, probe=probe, engine_sha=sha(probe), baseline="baseline",
        settings=settings, output=tmp_path / "qemu.log")
    assert "-dfilter" not in calibrated


@pytest.mark.parametrize("change", ["qemu", "probe", "flags", "library", "mode"])
def test_wrong_trace_runtime_rejected_before_launch(tmp_path, change):
    qemu, probe = tmp_path / "qemu", tmp_path / "probe"
    qemu.write_bytes(b"qemu");probe.write_bytes(b"probe")
    arguments = [str(qemu), "LD_LIBRARY_PATH=old", "baseline"]
    settings = dict(qemu_sha256=sha(qemu), mode="trace", center_base=BASE)
    engine_sha = sha(probe)
    if change == "qemu":settings["qemu_sha256"] = "0" * 64
    elif change == "probe":engine_sha = "0" * 64
    elif change == "flags":arguments += ["-d", "cpu"]
    elif change == "library":arguments[1] = "NO_LIBRARY=old"
    else:settings["mode"] = "other"
    with pytest.raises(ValueError):
        command_with_trace(arguments, probe=probe, engine_sha=engine_sha, baseline="baseline",
            settings=settings, output=tmp_path / "qemu.log")


def test_worker_binds_probe_to_ruleset_manifest(monkeypatch):
    import ast

    source = Path(__file__).with_name("trace_worker.py").read_text()
    module = ast.parse(source)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef)
        and node.name == "probe_environment")
    namespace = {"os": __import__("os")}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "trace_worker.py", "exec"), namespace)
    monkeypatch.setenv("CR_PROBE", "/unrelated/old-probe")
    actual = namespace["probe_environment"](dict(project="/frozen/project", probe="/frozen/libcrprobe.so"))
    assert actual["CR_PROBE"] == "/frozen/libcrprobe.so"
    assert actual["PYTHONPATH"] == "/frozen/project"


def test_only_isolated_supervisor_startup_deadline_is_extended(tmp_path):
    path = tmp_path / "service.py"
    original = "deadline = time.monotonic()+140\nother = 140\n"
    path.write_text(original)
    updated, digest = service_source(path)
    assert path.read_text() == original
    assert updated == "deadline = time.monotonic()+600\nother = 140\n"
    assert digest == __import__("hashlib").sha256(updated.encode()).hexdigest()
    path.write_text(original + original)
    with pytest.raises(ValueError, match="deadline"):
        service_source(path)


def test_engine_log_hard_limit_also_covers_startup(monkeypatch):
    import resource

    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda kind, limits: calls.append((kind, limits)))
    bound_output_files()
    assert calls == [(resource.RLIMIT_FSIZE, (256 * 1024**2, 256 * 1024**2))]
    monkeypatch.setattr(resource, "getrlimit", lambda kind: (1024, 1024))
    with pytest.raises(ValueError, match="too small"):
        bound_output_files()
    assert len(calls) == 1
