import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_live


ROOT = Path(__file__).resolve().parent
LAUNCH = "4434eedcfdf87ed083bf33aac916919e40d6bdb2e3891e73bfe1e7a20d5da5fe"


def arguments(attempt, launch_sha, receipt_sha, output):
    return SimpleNamespace(attempt=attempt, launch_sha=launch_sha, receipt_sha=receipt_sha,
        ancestor=ROOT.parent / "fair-final-random-execution-v1/native-execution-1792744646333119000",
        build=ROOT.parent / "fair-final-plugin-runtime-v1/build-1792746865344999000",
        snapshot_parent=ROOT.parent / "fair-final-global-rng-snapshot-v1/native-snapshot-1792734107610915000",
        library=ROOT.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so",
        output=output)


@pytest.mark.parametrize("name,launch,receipt", [
    ("native-plugin-1792907748579187000",
        "4ca304ffe1ba2316e689cef11f9ba12d0b7a517024921f46795a006c780a57a3",
        "e04d85df9fc285b26b1f9b5eaca0b3cf6b63bc5cfd704e52bc3e6487bbff9d9d"),
    ("native-plugin-1792908522802586000",
        "0cd62c9560cf96c19fbc8875cf07ad7aff3a221fa94ea41552514b1f96346e62",
        "f9330c44cbd83b7993e6da7767b84ede4558e85eef5e930d5fa05f5eeb5e71be"),
])
def test_actual_startup_failure_is_rejected_before_output(tmp_path, name, launch, receipt):
    attempt = ROOT / name
    if not (attempt / "download.json").exists():
        pytest.skip("private failed native archive excluded from public snapshot")
    output = tmp_path / "verification.json"
    run_live.configure()
    with pytest.raises(ValueError, match="missing evidence or archived failure"):
        run_live.verify(arguments(attempt, launch, receipt, output))
    assert not output.exists()


def test_actual_complete_archive_is_independently_recomputed(tmp_path, capsys):
    attempt = ROOT / "native-plugin-1792909386802583000"
    receipt = attempt / "download.json"
    if not receipt.exists():
        pytest.skip("private complete native archive not present")
    output = tmp_path / "verification.json"
    run_live.configure()
    run_live.verify(arguments(attempt, LAUNCH, run_live.sha(receipt), output))
    result = json.loads(output.read_text())
    assert json.loads(capsys.readouterr().out) == result
    assert result["status"] == "verified_limited_scope"
    assert result["comparison"]["compared_variants"] == 3
    assert result["trace_closure"] == "supervised_after_native_terminal"
    live = result["live_command"]
    assert live["executor_calls"] > 0
    assert live["ct86_helper_calls"] == live["after_helper_callbacks"] > 0
    assert live["executor_objects_seen_in_constructor"] == live["executor_calls"]
    assert live["virtual_dispatch_callsite_proven"] is live["gates_closed"] is False
    assert result["training_admissible"] is result["hidden_random_coverage_closed"] is False
