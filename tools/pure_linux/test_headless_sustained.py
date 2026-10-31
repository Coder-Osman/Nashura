"""Reject broken serial/terminal evidence and incomplete memory observations."""
import pytest

from summarize_headless_sustained import summarize
from test_headless_stability import sample


def fixture():
    first, rest = sample().split("HEADLESS_SERIAL_BATCH_BEGIN", 1)
    task = ("HEADLESS_SERIAL_BATCH_BEGIN" + rest).split(
        "HEADLESS_SERIAL_BATCH_END index=1 batches=2 status=1")[0]
    lines = [first]
    for batch in range(1, 7):
        chunk = task.replace("index=1 batches=2", f"index={batch} batches=6")
        for old in (11500, 12500, 13000, 14000):
            chunk = chunk.replace(f"={old}", f"={old + (batch - 1) * 10000}")
        chunk = chunk.replace("rss_kb=2048 pss_kb=1024",
                              f"rss_kb={2048 + batch * 10} pss_kb={1024 + batch * 5}")
        lines.extend([chunk, f"HEADLESS_SERIAL_BATCH_END index={batch} batches=6 status=1"] )
    lines.extend(["HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1", "HEADLESS_REJECTED_PM_CALLS=0",
                  "HEADLESS_BATTLE_READY=UNVERIFIED"])
    return chr(10).join(lines)


def run(text):
    return summarize(text, 6, 2, 3601, "6c959326dd2f09cc", 3091, 1)


def test_disjoint_windows_and_memory_trends():
    result = run(fixture())
    assert result["matches"] == 12 and result["measured_matches"] == 6
    assert [(x["first_batch"], x["last_batch"], x["measured_matches"])
            for x in result["windows"].values()] == [(1, 2, 2), (3, 4, 2), (5, 6, 2)]
    assert result["memory_trends"]["rss"]["last_minus_first_kib"] == 50
    assert result["memory_trends"]["pss"]["late_minus_early_median_kib"] == 20
    assert result["memory_trends"]["pss"]["ols_batch_median_kib_per_second"] > 0
    assert result["late_vs_early_duration_percent"] == 0
    assert result["whole_goal_complete"] is False


@pytest.mark.parametrize("old,new", [("pss_kb=1029", "pss_kb=-1"),
    ("pss_kb=1029", "absent_pss=1029"), ("rss_kb=2058", "rss_kb=0"),
    ("digest=6c959326dd2f09cc", "digest=0000000000000000"),
    ("HEADLESS_TASK_TID=123", "HEADLESS_TASK_TID=124"),
    ("index=6 batches=6 status=1", "index=6 batches=6 status=0")])
def test_missing_memory_or_failed_serial_gate_rejected(old, new):
    text = fixture().replace(old, new, 1)
    with pytest.raises(ValueError):
        run(text)


def test_two_tasks_are_insufficient_for_three_windows():
    with pytest.raises(ValueError, match="at least three"):
        summarize(sample(), 2, 2, 3601, "6c959326dd2f09cc", 3091, 1)
