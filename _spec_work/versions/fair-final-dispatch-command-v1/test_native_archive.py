import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_dispatch


ROOT = Path(__file__).resolve().parent


def arguments(attempt, launch_sha, receipt_sha, output):
    return SimpleNamespace(attempt=attempt, launch_sha=launch_sha, receipt_sha=receipt_sha,
        ancestor=ROOT.parent / "fair-final-random-execution-v1/native-execution-1792744646333119000",
        build=ROOT.parent / "fair-final-plugin-runtime-v1/build-1792746865344999000",
        snapshot_parent=ROOT.parent / "fair-final-global-rng-snapshot-v1/native-snapshot-1792734107610915000",
        library=ROOT.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so",
        output=output)


def test_old_complete_v3_archive_does_not_prove_x8_dispatch(tmp_path):
    attempt = ROOT.parent / "fair-final-live-command-v1/native-plugin-1792909386802583000"
    receipt = attempt / "download.json"
    if not receipt.exists():
        pytest.skip("private parent native archive excluded from public snapshot")
    output = tmp_path / "verification.json"
    run_dispatch.configure()
    with pytest.raises(ValueError, match="explicit v4 trace"):
        run_dispatch.verify(arguments(attempt,
            "4434eedcfdf87ed083bf33aac916919e40d6bdb2e3891e73bfe1e7a20d5da5fe",
            run_dispatch.sha(receipt), output))
    assert not output.exists()


def test_actual_complete_archive_is_recomputed(tmp_path, capsys):
    attempt = ROOT / "native-plugin-1792910823001223000"
    receipt = attempt / "download.json"
    if not receipt.exists():
        pytest.skip("private dispatch native archive not present")
    output = tmp_path / "verification.json"
    run_dispatch.configure()
    run_dispatch.verify(arguments(attempt,
        "0f11d08b34af0b84c80529c1be0417eec0fc8043bf8d57258a0fc9a4beb18a91",
        run_dispatch.sha(receipt), output))
    result = json.loads(output.read_text())
    assert result == json.loads(capsys.readouterr().out)
    assert result["status"] == "verified_limited_scope"
    assert result["comparison"]["compared_variants"] == 3
    dispatch = result["dispatch_command"]
    assert dispatch["paired_ct86_calls"] > 0
    assert dispatch["virtual_dispatch_callsite_proven"] is True
    assert dispatch["per_reservation_object_mapping_proven"] is False
    assert result["training_admissible"] is result["gates_closed"] is False
