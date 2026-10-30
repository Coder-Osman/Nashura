from pathlib import Path

import pytest

import run_headless_match as runner


COMPLETE = "\n".join((
    "HEADLESS_GAME_STATE_STEP_RETURN tick_before=0 tick_after=3601 completed=3601 requested=10000 finalized=1",
    "HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1",
    "HEADLESS_REJECTED_PM_CALLS=0",
    "HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1",
))


def test_native_final_and_normal_entry_completion_are_required():
    assert runner.verify_completion(0, COMPLETE) == 3601


@pytest.mark.parametrize("text", [
    "",
    COMPLETE.replace("finalized=1", "finalized=0"),
    COMPLETE.replace("tick_after=3601", "tick_after=0"),
    COMPLETE.replace("HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1", ""),
    COMPLETE.replace("HEADLESS_REJECTED_PM_CALLS=0", "HEADLESS_REJECTED_PM_CALLS=1"),
    COMPLETE + "\n" + COMPLETE.splitlines()[0],
])
def test_zero_exit_does_not_replace_runtime_evidence(text):
    with pytest.raises(ValueError):
        runner.verify_completion(0, text)


def test_nonzero_exit_preserves_failure_even_after_full_battle():
    with pytest.raises(ValueError, match="runtime exit code"):
        runner.verify_completion(70, COMPLETE)


def test_expected_repeated_native_finalizations_are_validated():
    step = COMPLETE.splitlines()[0]
    assert runner.verify_completion(0, COMPLETE + "\n" + step, expected_repeats=2) == 3601
    with pytest.raises(ValueError, match="finalization"):
        runner.verify_completion(0, COMPLETE + "\n" + step.replace("tick_after=3601", "tick_after=3600"),
                                 expected_repeats=2)


def test_changed_or_unpinned_input_rejected_before_output(tmp_path):
    closure = tmp_path / "closure"
    closure.mkdir()
    settings = {"closure": str(closure)}
    for name in ("namespace", "linker_config", "probe_dex", "observer", "apk", "replay"):
        settings[name] = str(tmp_path / name)
    settings["runtime"] = str(closure / "apex/com.android.art/bin/direct")
    settings["identities"] = {}
    with pytest.raises(ValueError, match="missing required input identity"):
        runner.build_plan(settings, tmp_path / "new-output")
    assert not (tmp_path / "new-output").exists()


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    closure = tmp_path / "closure"
    settings = {"closure": str(closure)}
    for name in ("namespace", "linker_config", "probe_dex", "observer", "apk", "replay"):
        settings[name] = str(tmp_path / name)
    settings["runtime"] = str(closure / "apex/com.android.art/bin/direct")
    required = [Path(value) for key, value in settings.items() if key != "closure"]
    required += [closure / name.lstrip("/") for name in runner.BOOT]
    required += [closure / name for name in (
        "data/local/tmp/current-game-classes.dex", "data/local/tmp/libg-input/libg.so",
        "data/local/tmp/libart_engine_jni_probe.so", "apex/com.android.runtime/bin/linker64",
        "apex/com.android.runtime/lib64/bionic/libc.so", "apex/com.android.art/lib64/libart.so",
        "system/lib64/libandroid_runtime.so", "system/lib64/libutils.so",
        "dev/__properties__/property_info", "dev/__properties__/properties_serial")]
    for path in required:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic input")
    Path(settings["runtime"]).chmod(0o755)
    settings["identities"] = {str(path): runner.sha256(path) for path in required}
    synthetic_sha = runner.sha256(required[0])
    for name in ("APK_SHA256", "DEX_SHA256", "ENGINE_SHA256"):
        monkeypatch.setattr(runner, name, synthetic_sha)
    monkeypatch.setattr(runner, "RUNTIME_SHA256", {name: synthetic_sha for name in runner.RUNTIME_SHA256})
    return settings, tmp_path / "output"


def test_plan_validates_without_creating_output_or_inheriting_service_environment(inputs):
    settings, output = inputs
    plan = runner.build_plan(settings, output)
    assert not output.exists()
    assert plan["whole_goal_complete"] is False
    assert "--probe-args" in plan["command"]
    assert "NASHURA_ROOT_INIT_TRIAL" not in plan["environment"]
    assert plan["snapshot_trace"] is True
    assert "NASHURA_TRACE_SNAPSHOTS" in plan["environment"]


def test_snapshot_trace_can_be_disabled_for_lightweight_diagnostics(inputs):
    settings, output = inputs
    plan = runner.build_plan(settings, output, snapshot_trace=False)
    assert plan["snapshot_trace"] is False
    assert plan["snapshot_scope"] == "not captured"
    assert "NASHURA_TRACE_SNAPSHOTS" not in plan["environment"]


def test_phase_timing_is_opt_in_for_guest_plan(inputs, monkeypatch):
    settings, output = inputs
    monkeypatch.delenv("NASHURA_PHASE_TIMING", raising=False)
    assert "NASHURA_PHASE_TIMING" not in runner.build_plan(settings, output)["environment"]
    monkeypatch.setenv("NASHURA_PHASE_TIMING", "1")
    assert runner.build_plan(settings, output)["environment"]["NASHURA_PHASE_TIMING"] == "1"


def test_battle_state_observations_are_opt_in(inputs, monkeypatch):
    settings, output = inputs
    monkeypatch.delenv("NASHURA_BATTLE_OBSERVATIONS", raising=False)
    assert runner.build_plan(settings, output)["environment"]["NASHURA_QUIET"] == "1"
    monkeypatch.setenv("NASHURA_BATTLE_OBSERVATIONS", "1")
    assert "NASHURA_QUIET" not in runner.build_plan(settings, output)["environment"]


def test_minimal_benchmark_plan_disables_trace_and_observers(inputs, monkeypatch):
    settings, output = inputs
    monkeypatch.setenv("NASHURA_PHASE_TIMING", "1")
    monkeypatch.setenv("NASHURA_BATTLE_OBSERVATIONS", "1")
    plan = runner.build_plan(settings, output, benchmark_repeats=20,
                             step_sample_every=16, timeout_seconds=180)
    assert plan["snapshot_trace"] is False
    assert plan["benchmark_repeats"] == 20
    assert plan["step_sample_every"] == 16
    assert plan["command"][3] == "180s"
    assert plan["environment"]["NASHURA_BENCHMARK_MINIMAL"] == "1"
    assert plan["environment"]["NASHURA_RESET_REPEATS"] == "20"
    assert plan["environment"]["NASHURA_MEMORY_SAMPLE"] == "1"
    assert plan["environment"]["NASHURA_TASK_TIMEOUT_MS"] == "120000"
    assert plan["environment"]["NASHURA_QUIET"] == "1"
    assert "NASHURA_TRACE_SNAPSHOTS" not in plan["environment"]
    assert "NASHURA_PHASE_TIMING" not in plan["environment"]


@pytest.mark.parametrize("timeout_seconds,expected_ms", [(5, 5000), (45, 45000), (120, 120000), (600, 120000)])
def test_serial_benchmark_task_wait_tracks_outer_bound_and_native_cap(inputs, timeout_seconds, expected_ms):
    settings, output = inputs
    plan = runner.build_plan(settings, output, benchmark_repeats=216, timeout_seconds=timeout_seconds)
    assert int(plan["environment"]["NASHURA_TASK_TIMEOUT_MS"]) == expected_ms
    assert "NASHURA_TASK_TIMEOUT_MS" not in runner.build_plan(settings, output)["environment"]


def test_mutation_after_identity_capture_is_rejected(inputs):
    settings, output = inputs
    Path(settings["observer"]).write_bytes(b"changed observer")
    with pytest.raises(ValueError, match="input identity mismatch"):
        runner.build_plan(settings, output)
    assert not output.exists()


def test_existing_output_and_unpinned_property_context_rejected(inputs):
    settings, output = inputs
    output.mkdir()
    with pytest.raises(ValueError, match="new output"):
        runner.build_plan(settings, output)
    extra = Path(settings["closure"]) / "dev/__properties__/extra-context"
    extra.write_bytes(b"untracked property input")
    with pytest.raises(ValueError, match="missing property input identity"):
        runner.build_plan(settings, output.parent / "other-output")
