"""Resource windows must match complete, validated warm-loop records."""
import pytest

from summarize_headless_resources import summarize
from test_summarize_headless_benchmark import repeated_log
from test_run_headless_match import inputs
import run_headless_match as runner


def sample():
    lines = []
    for iteration, start in ((1, 100), (2, 1400)):
        lines.append(repeated_log(iteration, start))
        lines.append(f"HEADLESS_WARM_RESOURCES scope=diagnostic-only boundary_start_ns={start - 10} boundary_end_ns={start + 1010} loop_start_ns={start} loop_end_ns={start + 1000} thread_cpu_ns=950 user_us=1 system_us=0 minor_faults=0 major_faults=0 voluntary_switches=0 involuntary_switches=1 input_blocks=0 output_blocks=0 valid=1")
    lines.append("HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=1")
    return chr(10).join(lines)


def test_resource_totals_exclude_warmup_and_keep_boundary_scope():
    result = summarize(sample(), 2, 3601, "6c959326dd2f09cc", 3091, 1)
    assert result["totals"]["thread_cpu_ns"] == 950
    assert result["totals"]["involuntary_switches"] == 1
    assert result["thread_cpu_fraction_of_boundary_wall"] == pytest.approx(950 / 1020)
    assert result["boundary_wall_minus_thread_cpu_ns"] == 70
    assert result["boundary_extra_ns"]["max"] == 20
    assert result["whole_goal_complete"] is False


@pytest.mark.parametrize("before,after", [
    ("valid=1", "valid=0"), ("scope=diagnostic-only", "scope=benchmark"),
    ("thread_cpu_ns=950", "thread_cpu_ns=1021"),
    ("minor_faults=0", "minor_faults=-1"),
    ("boundary_start_ns=90", "boundary_start_ns=110"),
    ("boundary_end_ns=1110", "boundary_end_ns=1090"),
    ("loop_start_ns=100", "loop_start_ns=101"),
    ("finalized=1", "finalized=0"),
])
def test_reject_invalid_resource_records(before, after):
    with pytest.raises(ValueError):
        summarize(sample().replace(before, after), 2, 3601, "6c959326dd2f09cc", 3091, 1)


def test_resource_records_cannot_be_missing_or_duplicated():
    text = sample()
    row = next(line for line in text.splitlines() if line.startswith("HEADLESS_WARM_RESOURCES "))
    for changed in (text.replace(row, ""), text + chr(10) + row):
        with pytest.raises(ValueError, match="one resource"):
            summarize(changed, 2, 3601, "6c959326dd2f09cc", 3091, 1)


def test_thread_resource_plan_is_explicit_and_repeated_only(inputs, monkeypatch):
    settings, output = inputs
    monkeypatch.setenv("NASHURA_THREAD_RESOURCES", "1")
    assert "NASHURA_THREAD_RESOURCES" not in runner.build_plan(settings, output)["environment"]
    with pytest.raises(ValueError, match="independent repeated"):
        runner.build_plan(settings, output, thread_resources=True)
    plan = runner.build_plan(settings, output, benchmark_repeats=216, thread_resources=True)
    assert plan["thread_resources"] is True
    assert plan["environment"]["NASHURA_THREAD_RESOURCES"] == "1"
    assert not plan["snapshot_trace"] and not plan["native_phases"]
