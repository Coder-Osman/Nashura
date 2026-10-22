import shutil
import subprocess

import pytest

from native_runner.paths import PACKAGE_ROOT


def test_native_phase_snapshot_roundtrip_and_rejections(tmp_path):
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler required")
    phase = (PACKAGE_ROOT.parent / "artifacts/standalone-resident-20260927/build/phase_runtime_telemetry.inc").read_text()
    payload = phase[phase.index("struct PhaseRuntimeCachePayload"):phase.index("struct NativePhaseSnapshot")]
    reset = phase[phase.index("void reset_phase_runtime_cache_payload"):phase.index("void commit_phase_runtime_epoch_locked")]
    cache = phase[phase.index("std::uint32_t phase_cache_native_id"):phase.index("bool phase_component_at")]
    harness = (PACKAGE_ROOT / "probe/test_fair_final_phase_snapshot.cpp").read_text()
    harness = harness.replace("FAIR_PHASE_PAYLOAD", payload).replace("FAIR_PHASE_RESET", reset).replace("FAIR_PHASE_CACHE", cache)
    source = tmp_path / "phase_snapshot.cpp"
    source.write_text(harness)
    binary = tmp_path / "phase_snapshot"
    subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(PACKAGE_ROOT / "probe"),
        str(source), "-o", str(binary)], check=True, capture_output=True, text=True)
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "phase snapshot checks passed"
