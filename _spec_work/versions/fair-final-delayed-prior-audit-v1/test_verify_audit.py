import json
from pathlib import Path
import shutil

import pytest

from verify_audit import digest, validate


ATTEMPT = Path(__file__).resolve().parent / "native-exact-audit-20261022"


def test_real_native_audit_replays_all_rows_and_exact_draw_states():
    result = validate(ATTEMPT)
    assert result["rows"] == 1044
    assert result["games"] == 3
    assert result["exact_native_states"] == 292
    assert result["support_failures"] == 0
    assert result["gates_closed"] is False


@pytest.mark.parametrize("mutation", ["summary", "admissible", "hidden_label", "missing_row",
    "prediction", "timing", "source", "stage"])
def test_tampering_real_evidence_is_rejected(tmp_path, mutation):
    attempt = tmp_path / "audit"
    shutil.copytree(ATTEMPT, attempt)
    report = json.loads((attempt / "report.json").read_text())
    if mutation in ("hidden_label", "missing_row", "prediction"):
        rows = json.loads((attempt / "private-audit.json").read_text())
        if mutation == "hidden_label":
            rows[0]["truth_next"] = rows[0]["truth_hand"][0]
        elif mutation == "missing_row":
            rows.pop()
        else:
            rows[0]["prediction"]["orders"] += 1
        (attempt / "private-audit.json").write_text(json.dumps(rows))
        report["private_audit_sha"] = digest(attempt / "private-audit.json")
    elif mutation == "summary":
        report["overall"]["equal_game_mean"]["next_brier"] = 0
    elif mutation == "admissible":
        report["training_admissible"] = True
    elif mutation == "timing":
        report["native_timing"]["exact_slot_fifo_states"] += 1
    elif mutation == "stage":
        report["by_stage"]["0-60s"]["rows"] += 1
    else:
        source = next((attempt / "source").rglob("delayed_cycle.py"))
        source.write_text("pass\n")
    (attempt / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        validate(attempt)
