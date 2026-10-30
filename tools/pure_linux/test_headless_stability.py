"""Validate ordered repeated tasks and a single Mainloop process lifetime."""
import pytest

import run_headless_match as runner
from summarize_headless_stability import summarize
from test_run_headless_match import inputs
from test_summarize_headless_benchmark import repeated_log


def sample():
    lines = ["HEADLESS_CREATE_RETURN_NULL=false"]
    done = ("HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1", "HEADLESS_REJECTED_PM_CALLS=0",
            "HEADLESS_BATTLE_READY=UNVERIFIED")
    for batch in (1, 2):
        lines.extend([f"HEADLESS_SERIAL_BATCH_BEGIN index={batch} batches=2", "HEADLESS_TASK_TID=123"])
        for repeat in (1, 2):
            log = repeated_log(repeat, batch * 10000 + repeat * 1500)
            lines.extend(line for line in log.splitlines() if line not in done)
        lines.extend(["HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1",
                      f"HEADLESS_SERIAL_BATCH_END index={batch} batches=2 status=1"])
    lines.extend(done)
    return chr(10).join(lines)


def test_single_lifetime_serial_tasks_and_memory():
    result = summarize(sample(), 2, 2, 3601, "6c959326dd2f09cc", 3091, 1)
    assert result["matches"] == 4 and result["measured_matches"] == 2
    assert result["rss_kib"]["last_minus_first"] == 0
    assert result["first_to_last_loop_seconds"] == 12500 / 1e9
    assert result["whole_goal_complete"] is False


@pytest.mark.parametrize("before,after", [
    ("index=2 batches=2 status=1", "index=1 batches=2 status=1"),
    ("HEADLESS_CREATE_RETURN_NULL=false", ""),
    ("status=1", "status=0"),
    ("HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1", "HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=-5"),
    ("digest=6c959326dd2f09cc", "digest=0000000000000000"),
])
def test_reject_invalid_stability_evidence(before, after):
    with pytest.raises(ValueError):
        summarize(sample().replace(before, after), 2, 2, 3601, "6c959326dd2f09cc", 3091, 1)


def test_different_mainloop_tid_and_diagnostics_are_rejected():
    text = sample()
    at = text.rfind("HEADLESS_TASK_TID=123")
    changed = text[:at] + text[at:].replace("HEADLESS_TASK_TID=123", "HEADLESS_TASK_TID=124", 1)
    for log in (changed, text + chr(10) + "HEADLESS_WARM_RESOURCES scope=diagnostic-only"):
        with pytest.raises(ValueError):
            summarize(log, 2, 2, 3601, "6c959326dd2f09cc", 3091, 1)


def test_both_native_create_return_forms_are_preserved():
    for value in ("true", "false"):
        text = sample().replace("HEADLESS_CREATE_RETURN_NULL=false", "HEADLESS_CREATE_RETURN_NULL=" + value)
        assert summarize(text, 2, 2, 3601, "6c959326dd2f09cc", 3091, 1)["matches"] == 4
    for value in ("invalid", "false\nHEADLESS_CREATE_RETURN_NULL=true"):
        with pytest.raises(ValueError, match="lifecycle"):
            summarize(sample().replace("HEADLESS_CREATE_RETURN_NULL=false",
                      "HEADLESS_CREATE_RETURN_NULL=" + value), 2, 2, 3601, "6c959326dd2f09cc", 3091, 1)


def test_serial_plan_is_bounded_and_does_not_inherit_environment(inputs, monkeypatch):
    settings, output = inputs
    monkeypatch.setenv("NASHURA_SERIAL_BATCHES", "240")
    assert "NASHURA_SERIAL_BATCHES" not in runner.build_plan(settings, output)["environment"]
    plan = runner.build_plan(settings, output, benchmark_repeats=516, serial_batches=20, timeout_seconds=600)
    assert plan["serial_batches"] == 20
    assert plan["environment"]["NASHURA_SERIAL_BATCHES"] == "20"
    assert plan["environment"]["NASHURA_TASK_TIMEOUT_MS"] == "120000"
    for kwargs in ({"serial_batches": 0}, {"serial_batches": 241}, {"serial_batches": 2},
                   {"serial_batches": 2, "benchmark_repeats": 116, "thread_resources": True}):
        with pytest.raises(ValueError, match="serial batches"):
            runner.build_plan(settings, output, **kwargs)


@pytest.mark.parametrize("extra", ["HEADLESS_TASK_TID=123",
    "HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1"])
def test_unscoped_duplicate_task_evidence_is_rejected(extra):
    with pytest.raises(ValueError, match="lifecycle"):
        summarize(sample() + chr(10) + extra, 2, 2, 3601, "6c959326dd2f09cc", 3091, 1)


def test_create_must_precede_tasks_and_memory_must_be_present():
    text = sample()
    late_create = text.replace("HEADLESS_CREATE_RETURN_NULL=false", "") + chr(10) + "HEADLESS_CREATE_RETURN_NULL=false"
    with pytest.raises(ValueError, match="lifecycle"):
        summarize(late_create, 2, 2, 3601, "6c959326dd2f09cc", 3091, 1)
    with pytest.raises(ValueError, match="memory sample"):
        summarize(text.replace("HEADLESS_BATTLE_MEMORY rss_kb=2048",
                               "HEADLESS_BATTLE_MEMORY rss_kb=-1"),
                  2, 2, 3601, "6c959326dd2f09cc", 3091, 1)


def test_setup_tasks_precede_serial_batches_on_same_mainloop():
    text = "HEADLESS_TASK_TID=123" + chr(10) + sample()
    assert summarize(text, 2, 2, 3601, "6c959326dd2f09cc", 3091, 1)["matches"] == 4
    with pytest.raises(ValueError, match="Mainloop thread changed"):
        summarize(text.replace("HEADLESS_TASK_TID=123", "HEADLESS_TASK_TID=122", 1),
                  2, 2, 3601, "6c959326dd2f09cc", 3091, 1)
