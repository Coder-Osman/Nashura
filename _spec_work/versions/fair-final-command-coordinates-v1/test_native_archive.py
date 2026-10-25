import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import run_coordinates


def test_actual_complete_coordinate_archive_is_recomputed(tmp_path, capsys):
    attempt = ROOT / "native-plugin-1792914264812898000"
    receipt = attempt / "download.json"
    if not receipt.exists():
        pytest.skip("private coordinate native archive not present")
    output = tmp_path / "verification.json"
    run_coordinates.configure()
    run_coordinates.verify(SimpleNamespace(attempt=attempt,
        launch_sha="884e03ff0b2776804586f4f27557079a2e2c6bb7e522736da0737b402f8b8312",
        receipt_sha=run_coordinates.run_mapping.run_dispatch.sha(receipt), output=output,
        ancestor=ROOT.parent / "fair-final-random-execution-v1/native-execution-1792744646333119000",
        build=ROOT.parent / "fair-final-plugin-runtime-v1/build-1792746865344999000",
        snapshot_parent=ROOT.parent / "fair-final-global-rng-snapshot-v1/native-snapshot-1792734107610915000",
        library=ROOT.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"))
    result = json.loads(output.read_text())
    assert result == json.loads(capsys.readouterr().out)
    assert result["status"] == "verified_limited_scope"
    assert result["comparison"]["compared_variants"] == 3
    coordinates = result["command_coordinates"]
    assert coordinates["paired_reservations"] == result["reservation_mapping"]["mapped_reservations"]
    assert coordinates["paired_reservations"] > 0
    assert coordinates["placement_offset_reservations"] > 0
    assert coordinates["executor_loaded_coordinates_proven"] is True
    assert result["training_admissible"] is result["gates_closed"] is False
