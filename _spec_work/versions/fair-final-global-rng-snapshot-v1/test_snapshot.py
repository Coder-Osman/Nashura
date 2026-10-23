import copy
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

from build_engine import BASE, ROOT, phase_source, replace_once, resident_source, sha, stage_source
from verify_snapshot import SCOPE, validate, validate_probe
from global_rng_audit import LIBG_SHA, STATE_SIZE, perturb_state, state_summary
from run_snapshot import PORT, upload_sources


@pytest.fixture(scope="module")
def native_harness(tmp_path_factory):
    compiler = shutil.which("clang++") or shutil.which("g++")
    baseline = BASE / "resident.cpp"
    if not compiler or not baseline.exists():
        pytest.skip("C++ compiler and baseline native source required")
    source = baseline.read_text()
    hashing = source[source.index("struct Sha256Context {"):source.index("bool sha256_file(")]
    folder = tmp_path_factory.mktemp("global-snapshot-cpp")
    harness = folder / "harness.cpp"
    harness.write_text(r'''
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cstdio>
#include <cstdlib>
#include <unistd.h>
#include <vector>
constexpr int PF_R = 4;
constexpr int PF_W = 2;
std::uintptr_t g_libg_base = 0;
const char *g_libg_path = "fixture";
bool writable = true;
bool single_lane = true;
bool library_valid = true;
bool libg_address_has_segment_flags(void *, std::size_t, int flags) {
  return writable && flags == (PF_R | PF_W);
}
''' + hashing + r'''
bool sha256_file(const char *, char output[65]) {
  std::strcpy(output, library_valid ? "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783" : "wrong");
  return true;
}
''' + (ROOT / "global_snapshot.inc").read_text() + r'''
bool fair_final_global_single_lane(void *manager) { return manager != nullptr && single_lane; }
int main(int argc, char **argv) {
  if (argc != 2) return 2;
  const char *mode = argv[1];
  std::vector<std::uint8_t> memory(kFairFinalGlobalMtSize);
  for (std::size_t index = 0; index < memory.size(); ++index) memory[index] = index % 251;
  const auto original = memory;
  g_libg_base = reinterpret_cast<std::uintptr_t>(memory.data()) - kFairFinalGlobalMtOffset;
  int owner = 0, other = 0;
  FairFinalGlobalRngSnapshot saved;
  if (std::strcmp(mode, "wrong-library") == 0) library_valid = false;
  if (std::strcmp(mode, "readonly-capture") == 0) writable = false;
  if (std::strcmp(mode, "multi-capture") == 0) single_lane = false;
  if (!library_valid || !writable || !single_lane) {
    return !capture_fair_final_global_rng(&owner, 1, &saved) && memory == original ? 0 : 3;
  }
  if (!capture_fair_final_global_rng(&owner, 1, &saved)) return 4;
  if (std::strcmp(mode, "digest") == 0) {
    const std::uint8_t payload[] = {0, 1, 2, 255};
    char digest[65] = {};
    if (!fair_final_global_digest(&owner, payload, sizeof(payload), digest)) return 5;
    std::puts(digest);
    return 0;
  }
  if (std::strcmp(mode, "invalid") == 0) {
    char digest[65] = {};
    if (capture_fair_final_global_rng(&owner, 0, &saved) || capture_fair_final_global_rng(&owner, 1, nullptr) ||
        fair_final_global_digest(&owner, nullptr, 4, digest) || fair_final_global_digest(&owner, memory.data(), 0, digest) ||
        fair_final_global_digest(&owner, memory.data(), 32 * 1024 * 1024, digest) ||
        fair_final_global_digest(&owner, memory.data(), 4, nullptr)) return 6;
    return memory == original ? 0 : 7;
  }
  memory.assign(memory.size(), 173);
  const auto changed = memory;
  if (std::strcmp(mode, "roundtrip") == 0) {
    for (int repeat = 0; repeat < 3; ++repeat) {
      if (!restore_fair_final_global_rng(&owner, 1, saved) || memory != original) return 8;
      if (repeat != 2) memory.assign(memory.size(), 173);
    }
    return 0;
  }
  if (std::strcmp(mode, "pid") == 0) ++saved.process;
  else if (std::strcmp(mode, "base") == 0) ++saved.libg_base;
  else if (std::strcmp(mode, "live-base") == 0) ++g_libg_base;
  else if (std::strcmp(mode, "manager") == 0) saved.manager = &other;
  else if (std::strcmp(mode, "generation") == 0) ++saved.generation;
  else if (std::strcmp(mode, "bytes") == 0) saved.bytes[2503] ^= 1;
  else if (std::strcmp(mode, "sha") == 0) saved.sha256[0] ^= 1;
  else if (std::strcmp(mode, "terminator") == 0) saved.sha256[64] = 'x';
  else if (std::strcmp(mode, "readonly") == 0) writable = false;
  else if (std::strcmp(mode, "multi") == 0) single_lane = false;
  else return 9;
  return !restore_fair_final_global_rng(&owner, 1, saved) && memory == changed ? 0 : 10;
}
''')
    executable = folder / "harness"
    subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", str(harness), "-o", str(executable)],
        capture_output=True, text=True, timeout=60, check=True)
    return executable


@pytest.mark.parametrize("mode", ["roundtrip", "pid", "base", "live-base", "manager", "generation", "bytes",
    "sha", "terminator", "readonly", "multi", "wrong-library", "readonly-capture", "multi-capture", "invalid"])
def test_native_module_restores_or_rejects_without_writing(native_harness, mode):
    subprocess.run([str(native_harness), mode], capture_output=True, text=True, timeout=10, check=True)


def test_native_digest_matches_independent_sha256_reference(native_harness):
    content = bytes(index % 251 for index in range(STATE_SIZE))
    payload = bytes([0, 1, 2, 255])
    expected = hashlib.sha256(b"fair-final-state-with-global-mt.v1" + struct.pack("<Q", len(payload)) + payload + content).hexdigest()
    result = subprocess.run([str(native_harness), "digest"], capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("source", ["absent", "duplicated duplicated"])
def test_patch_rejects_missing_or_ambiguous_anchor(source):
    with pytest.raises(ValueError, match="anchor differs"):
        replace_once(source, "duplicated", "replacement")


def test_build_patches_prevalidate_before_deserialization_and_cover_phase():
    if not (BASE / "resident.cpp").exists():
        pytest.skip("private build baseline omitted")
    resident = resident_source((BASE / "resident.cpp").read_text())
    assert resident.index("if (!validate_fair_final_phase_global(manager, saved.generation") < resident.index(
        "void *const stream = static_cast<std::uint8_t *>(saved.wrapper) + 0x08;")
    phase = phase_source((BASE / "probe/fair_final_phase_snapshot.inc").read_text())
    assert "capture_fair_final_global_rng(manager, generation, &saved->global_rng)" in phase
    assert "return restore_fair_final_global_rng(manager, generation, saved->global_rng);" in phase
    assert SCOPE in stage_source((BASE / "stage_commands.inc").read_text())


def test_real_single_lane_guard_rejects_render_and_shared_slots(tmp_path):
    compiler = shutil.which("clang++") or shutil.which("g++")
    baseline = BASE / "resident.cpp"
    if not compiler or not baseline.exists():
        pytest.skip("native baseline and C++ compiler required")
    resident = resident_source(baseline.read_text())
    start = resident.index("bool fair_final_global_single_lane(void *manager) {")
    end = resident.index("\n}", start) + 2
    source = tmp_path / "lane.cpp"
    source.write_text(r'''
#include <atomic>
#include <cstdint>
#include <cstddef>
#include <vector>
enum class RunnerMode { Headless = 1, Render = 2 };
std::atomic<std::int32_t> g_runner_mode{1};
void *g_controlled_manager = nullptr;
struct Slot { bool occupied; void *manager; };
std::vector<Slot> g_resident_slots;
''' + resident[start:end] + r'''
int main() {
  int owner = 0, other = 0;
  g_controlled_manager = &owner;
  if (!fair_final_global_single_lane(&owner) || fair_final_global_single_lane(nullptr) ||
      fair_final_global_single_lane(&other)) return 1;
  g_runner_mode = 2;
  if (fair_final_global_single_lane(&owner)) return 2;
  g_runner_mode = 1;
  g_resident_slots = {{true, &owner}, {false, &other}};
  if (!fair_final_global_single_lane(&owner)) return 3;
  g_resident_slots[1].occupied = true;
  if (fair_final_global_single_lane(&owner)) return 4;
  g_resident_slots = {{true, &other}};
  return fair_final_global_single_lane(&owner) ? 5 : 0;
}
''')
    executable = tmp_path / "lane"
    subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", str(source), "-o", str(executable)],
        capture_output=True, text=True, timeout=60, check=True)
    subprocess.run([str(executable)], capture_output=True, text=True, timeout=10, check=True)


def probe_fixture():
    process = dict(child_pid=17, child_start_ticks="91")
    original = bytes(STATE_SIZE)
    def census(tick, epoch):
        return dict(**process, libg_sha256=LIBG_SHA, global_offset="0x19fc368", readonly=True,
            state=state_summary(original), generation=1, stateEpoch=epoch, tick=tick)
    def digest(tick):
        return dict(tick=tick, scope=SCOPE, sha256="a" * 64, nativeSha256="b" * 64)
    rows = []
    for tick in (90, 1200, 3600, 5400):
        before = digest(tick)
        rows.append(dict(threshold=tick, original_hex=original.hex(), perturbed_hex=perturb_state(original).hex(),
            restored_hex=original.hex(), repeated_hex=original.hex(), before=census(tick, 2), restored=census(tick, 3),
            repeated=census(tick, 5), before_digest=before, perturbed_digest={**before, "sha256": "c" * 64},
            final_digest=copy.deepcopy(before), branches=[[digest(tick + step) for step in range(1, 11)] for _ in range(2)],
            snapshot_released=True, no_native_steps_while_perturbed=True))
    return dict(schema="fair-final-global-rng-snapshot-probe.v1", status="completed_limited_scope", second_process=False,
        engine_sha256="d" * 64, startup=census(0, 1), startup_hex=original.hex(), records=rows,
        fixed_hog_usage_proven=False, hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False), process


def test_probe_limited_scope():
    report, process = probe_fixture()
    validate_probe(report, process, second_process=False, engine_sha="d" * 64)


@pytest.mark.parametrize("mutation", ["gates", "bytes", "perturbation", "epoch", "generation", "pid", "tick",
    "digest", "native-digest", "scope", "branch", "branch-length", "branch-tick", "release", "steps", "stage"])
def test_probe_rejects_semantic_tampering(mutation):
    report, process = probe_fixture()
    row = report["records"][0]
    if mutation == "gates":
        report["gates_closed"] = True
    elif mutation == "bytes":
        row["repeated_hex"] = row["perturbed_hex"]
    elif mutation == "perturbation":
        row["perturbed_hex"] = row["original_hex"]
    elif mutation == "epoch":
        row["repeated"]["stateEpoch"] = row["restored"]["stateEpoch"]
    elif mutation == "generation":
        row["repeated"]["generation"] += 1
    elif mutation == "pid":
        row["repeated"]["child_pid"] += 1
    elif mutation == "tick":
        row["restored"]["tick"] += 1
    elif mutation == "digest":
        row["perturbed_digest"]["sha256"] = row["before_digest"]["sha256"]
    elif mutation == "native-digest":
        row["perturbed_digest"]["nativeSha256"] = "f" * 64
    elif mutation == "scope":
        row["before_digest"]["scope"] = "whole-process"
    elif mutation == "branch":
        row["branches"][1][0]["sha256"] = "f" * 64
    elif mutation == "branch-length":
        row["branches"][0].pop()
    elif mutation == "branch-tick":
        for branch in row["branches"]:
            branch[0]["tick"] += 1
    elif mutation == "release":
        row["snapshot_released"] = False
    elif mutation == "steps":
        row["no_native_steps_while_perturbed"] = False
    else:
        report["records"].pop()
    with pytest.raises(ValueError):
        validate_probe(report, process, second_process=False, engine_sha="d" * 64)


def archive_fixture(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    def write(name, value):
        path = archive / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        return sha(path)
    sources = {name: write(name, content) for name, content in upload_sources().items()}
    engine_sha = write("libcrprobe.so", b"synthetic engine fixture")
    settings = dict(port=PORT, probe="/fixture/probe", engine_sha256=engine_sha, source_sha256=dict(sources))
    identities = {**sources, "libcrprobe.so": engine_sha, "settings.json": write("settings.json", settings)}
    frozen = {"fixture-runtime.py": "a" * 64}
    for second, variant in enumerate(("immediate", "delayed")):
        report, process = probe_fixture()
        process["child_pid"] += second
        for census in [report["startup"], *[row[name] for row in report["records"]
                for name in ("before", "restored", "repeated")]]:
            census["child_pid"] = process["child_pid"]
        process.update(phase="stopped", phase_snapshot_engine=dict(path=settings["probe"], sha256=engine_sha))
        report.update(second_process=bool(second), source_sha256=frozen, engine_sha256=engine_sha)
        write(variant + "/engine/process.json", process)
        write(variant + "/cleanup.json", dict(exit_code=0, error=None, clean=True, listening=False,
            pids_alive=dict(pid=False, child_pid=False), port=PORT, process=process))
        write(variant + "/report.json", report)
        write(variant + "/progress.json", dict(records=report["records"]))
    write("worker.log", b"")
    build = dict(schema="fair-final-global-rng-snapshot-build.v1", builder_sha256=sha(ROOT / "build_engine.py"),
        module_sha256=sha(ROOT / "global_snapshot.inc"), engine_sha256=engine_sha,
        artifacts={"probe/global_snapshot.inc": sha(ROOT / "global_snapshot.inc"), "libcrprobe.so": engine_sha},
        training_admissible=False, gates_closed=False)
    launch = dict(schema="fair-final-global-rng-snapshot-launch.v1", settings=settings, uploaded_sha256=identities,
        runner_sha256=sha(ROOT / "run_snapshot.py"), contract_sha256=sha(ROOT / "CONTRACT.md"),
        build_manifest=build, build_manifest_sha256=hashlib.sha256((json.dumps(build, indent=2) + "\n").encode()).hexdigest(),
        frozen_runtime_sha256=frozen, training_admissible=False, gates_closed=False)
    (tmp_path / "launch.json").write_text(json.dumps(launch))
    launch_sha = sha(tmp_path / "launch.json")
    def receipt():
        record = dict(schema="fair-final-global-rng-snapshot-download.v1", launch_sha256=launch_sha,
            files={str(path.relative_to(archive)): sha(path) for path in archive.rglob("*") if path.is_file()},
            source_worker_stopped=True, training_admissible=False, gates_closed=False)
        (tmp_path / "download.json").write_text(json.dumps(record))
        return sha(tmp_path / "download.json")
    return archive, launch_sha, receipt


def test_archive_verifies_two_native_processes_without_closing_gate(tmp_path):
    archive, launch_sha, receipt = archive_fixture(tmp_path)
    result = validate(tmp_path, launch_sha, receipt())
    assert result["tested_boundaries"] == 8 and result["native_processes"] == 2
    assert not result["gates_closed"] and not result["training_admissible"]


@pytest.mark.parametrize("mutation", ["failure", "cleanup", "source", "binary", "runtime", "progress", "missing", "branch"])
def test_archive_rejects_tampering_even_with_recomputed_receipt(tmp_path, mutation):
    archive, launch_sha, receipt = archive_fixture(tmp_path)
    if mutation == "failure":
        (archive / "immediate/failure.json").write_text("{}")
    elif mutation in ("source", "binary"):
        path = archive / ("probe_seed.py" if mutation == "source" else "libcrprobe.so")
        path.write_bytes(path.read_bytes() + b"changed")
    elif mutation == "missing":
        (archive / "delayed/report.json").unlink()
    else:
        path = archive / ("delayed/cleanup.json" if mutation == "cleanup" else
            "delayed/progress.json" if mutation == "progress" else "delayed/report.json")
        content = json.loads(path.read_text())
        if mutation == "cleanup":
            content["pids_alive"]["child_pid"] = True
        elif mutation == "runtime":
            content["source_sha256"] = {}
        elif mutation == "branch":
            content["records"][0]["branches"][1][0]["sha256"] = "f" * 64
            (archive / "delayed/progress.json").write_text(json.dumps(dict(records=content["records"])))
        else:
            content["records"].pop()
        path.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        validate(tmp_path, launch_sha, receipt())


def test_archive_rejects_wrong_external_hashes(tmp_path):
    archive, launch_sha, receipt = archive_fixture(tmp_path)
    receipt_sha = receipt()
    for launch_id, receipt_id in (("0" * 64, receipt_sha), (launch_sha, "0" * 64)):
        with pytest.raises(ValueError, match="external identities"):
            validate(tmp_path, launch_id, receipt_id)
