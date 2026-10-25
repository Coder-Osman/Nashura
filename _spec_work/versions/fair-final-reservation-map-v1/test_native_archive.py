import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_mapping
from reservation_audit import validate_mapping


ROOT = Path(__file__).resolve().parent


def test_old_complete_dispatch_archive_has_no_reservation_intervals():
    archive = ROOT.parent / "fair-final-dispatch-command-v1/native-plugin-1792910823001223000/archive/plugin"
    if not archive.exists():
        pytest.skip("private parent native archive excluded from public snapshot")
    read = lambda name: json.loads((archive / name).read_text())
    with pytest.raises(ValueError, match="interval schema"):
        validate_mapping(read("trace-summary.json"), read("report.json"), read("measurements.json"))


def test_actual_complete_reservation_archive_is_recomputed(tmp_path, capsys):
    attempt = ROOT / "native-plugin-1792912707427676000"
    receipt = attempt / "download.json"
    if not receipt.exists():
        pytest.skip("private reservation native archive not present")
    output = tmp_path / "verification.json"
    run_mapping.configure()
    run_mapping.verify(SimpleNamespace(attempt=attempt,
        launch_sha="f58f44028512add7e945b5ea9447208e99caa147e15fedb11cc04e1e28974baa",
        receipt_sha=run_mapping.run_dispatch.sha(receipt), output=output,
        ancestor=ROOT.parent / "fair-final-random-execution-v1/native-execution-1792744646333119000",
        build=ROOT.parent / "fair-final-plugin-runtime-v1/build-1792746865344999000",
        snapshot_parent=ROOT.parent / "fair-final-global-rng-snapshot-v1/native-snapshot-1792734107610915000",
        library=ROOT.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"))
    result = json.loads(output.read_text())
    assert result == json.loads(capsys.readouterr().out)
    assert result["status"] == "verified_limited_scope"
    assert result["comparison"]["compared_variants"] == 3
    mapping = result["reservation_mapping"]
    assert mapping["mapped_reservations"] > 0
    assert mapping["per_reservation_object_mapping_proven"] is True
    assert result["training_admissible"] is result["gates_closed"] is False
