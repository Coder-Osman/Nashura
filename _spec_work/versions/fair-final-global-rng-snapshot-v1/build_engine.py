import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
BASE = WORKSPACE / "_spec_work/evidence/fair-final-v1-20261019/phase-snapshot-engine-1792485763481586000"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("global snapshot build anchor differs: " + before[:120])
    return source.replace(before, after, 1)


def phase_source(source):
    source = replace_once(source, "struct FairFinalPhaseSnapshot {",
        "struct FairFinalPhaseSnapshot {\n  FairFinalGlobalRngSnapshot global_rng;")
    source = replace_once(source, "  saved->tick = tick;",
        "  saved->tick = tick;\n"
        "  if (!capture_fair_final_global_rng(manager, generation, &saved->global_rng)) return nullptr;")
    source = replace_once(source, "  void *matched[kMaxObservedObjects] = {};",
        "  if (!validate_fair_final_global_rng(manager, generation, saved->global_rng)) return false;\n"
        "  void *matched[kMaxObservedObjects] = {};")
    before, separator, after = source.rpartition("  return true;\n}")
    if not separator or after.strip():
        raise ValueError("global snapshot phase restore tail differs")
    return before + "  return restore_fair_final_global_rng(manager, generation, saved->global_rng);\n}" + after


def resident_source(source):
    source = replace_once(source, "struct StoredSnapshot {",
        "bool validate_fair_final_phase_global(void *, std::uint64_t,\n"
        "    const std::shared_ptr<const FairFinalPhaseSnapshot> &);\n\nstruct StoredSnapshot {")
    source = replace_once(source, '#include "fair_final_phase_snapshot.inc"',
        '#include "global_snapshot.inc"\n#include "fair_final_phase_snapshot.inc"\n\n'
        "bool validate_fair_final_phase_global(void *manager, std::uint64_t generation,\n"
        "    const std::shared_ptr<const FairFinalPhaseSnapshot> &saved) {\n"
        "  return saved && validate_fair_final_global_rng(manager, generation, saved->global_rng);\n}\n")
    source = replace_once(source, '    void *const stream = static_cast<std::uint8_t *>(saved.wrapper) + 0x08;',
        "    if (!validate_fair_final_phase_global(manager, saved.generation, saved.phase_snapshot)) {\n"
        "      pthread_mutex_lock(&g_control_mutex);\n"
        "      complete_snapshot_operation_locked(sequence, false, SnapshotError::RestoreVerificationFailed);\n"
        "      pthread_mutex_unlock(&g_control_mutex);\n"
        "      return;\n    }\n\n"
        "    void *const stream = static_cast<std::uint8_t *>(saved.wrapper) + 0x08;")
    source = replace_once(source, '#include "resident_multimatch.inc"',
        '#include "resident_multimatch.inc"\n\n'
        "bool fair_final_global_single_lane(void *manager) {\n"
        "  if (manager == nullptr || manager != g_controlled_manager ||\n"
        "      g_runner_mode.load(std::memory_order_acquire) != static_cast<std::int32_t>(RunnerMode::Headless))\n"
        "    return false;\n"
        "  std::size_t occupied = 0;\n"
        "  for (const auto &slot : g_resident_slots) {\n"
        "    if (!slot.occupied) continue;\n"
        "    if (slot.manager != manager || ++occupied > 1) return false;\n"
        "  }\n  return true;\n}\n")
    return source


def stage_source(source):
    source = replace_once(source, "  void *snapshot = battle_probe_snapshot_wrapper(g_libg_base, g_controlled_manager);",
        "  FairFinalGlobalRngSnapshot before_global;\n"
        "  const bool global_captured = capture_fair_final_global_rng(\n"
        "      g_controlled_manager, g_control_generation, &before_global);\n"
        "  void *snapshot = global_captured ? battle_probe_snapshot_wrapper(g_libg_base, g_controlled_manager) : nullptr;")
    source = replace_once(source,
        "  const bool valid = bytes && size > 0 && size < 32 * 1024 * 1024 && sha256_bytes(bytes, size, digest);",
        "  char native_digest[65] = {};\n"
        "  std::uint8_t *global_address = nullptr;\n"
        "  const bool valid = global_captured && bytes && size > 0 && size < 32 * 1024 * 1024 &&\n"
        "      fair_final_global_address(g_controlled_manager, &global_address) &&\n"
        "      std::memcmp(global_address, before_global.bytes, sizeof(before_global.bytes)) == 0 &&\n"
        "      sha256_bytes(bytes, size, native_digest) &&\n"
        "      fair_final_global_digest(g_controlled_manager, bytes, size, digest);")
    source = replace_once(source, "  char response[256];", "  char response[512];")
    source = replace_once(source,
        '      "{\\"ok\\":%s,\\"tick\\":%d,\\"bytes\\":%d,\\"sha256\\":\\"%s\\"}",\n'
        '      valid ? "true" : "false", g_game_state_tick(g_controlled_manager), size, digest);',
        '      "{\\"ok\\":%s,\\"tick\\":%d,\\"bytes\\":%d,\\"sha256\\":\\"%s\\",\"'
        '\n      "\\"nativeSha256\\":\\"%s\\",\\"scope\\":\\"native-payload+global-mt2504.v1\\"}",\n'
        '      valid ? "true" : "false", g_game_state_tick(g_controlled_manager), size, digest, native_digest);')
    return source


def build():
    manifest = json.loads((BASE / "build-manifest.json").read_text())
    for name, expected in manifest["artifacts"].items():
        if sha(BASE / name) != expected:
            raise ValueError("global snapshot baseline build differs")
    output = ROOT / ("build-" + str(time.time_ns()))
    output.mkdir()
    for path in BASE.rglob("*"):
        if path.is_file() and path.suffix in {".inc", ".h", ".cpp", ".c", ".S"}:
            target = output / path.relative_to(BASE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    (output / "probe/global_snapshot.inc").write_bytes((ROOT / "global_snapshot.inc").read_bytes())
    for relative, transform in (("resident.cpp", resident_source),
            ("probe/fair_final_phase_snapshot.inc", phase_source), ("stage_commands.inc", stage_source)):
        target = output / relative
        target.write_text(transform(target.read_text()))
    compiler_dir = Path.home() / "Library/Android/sdk/ndk/27.3.13750724/toolchains/llvm/prebuilt/darwin-x86_64/bin"
    common = ["-O2", "-fPIC", "-Wall", "-Wextra", "-Werror"]
    commands = [
        [str(compiler_dir / "aarch64-linux-android30-clang"), *common, "-c", str(output / "battle_probe.c"),
            "-o", str(output / "battle_probe.o")],
        [str(compiler_dir / "aarch64-linux-android30-clang++"), "-std=c++17", *common,
            "-DCR_STANDALONE_HOST", "-fvisibility=hidden", "-shared", "-Wl,-z,max-page-size=16384",
            "-I", str(output / "probe"), "-I", str(output), str(output / "resident.cpp"),
            str(output / "battle_probe.o"), str(output / "call_sret.S"),
            str(output / "probe/native_touch_interceptor.cpp"), str(output / "probe/native_call_arm64.S"),
            str(output / "probe/remaining_runtime_arm64.S"), "-llog", "-ldl", "-lz",
            "-o", str(output / "libcrprobe.so")],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        with (output / "build.log").open("a") as stream:
            stream.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("global snapshot build failed: " + str(output))
    record = dict(schema="fair-final-global-rng-snapshot-build.v1", baseline_sha256=sha(BASE / "build-manifest.json"),
        builder_sha256=sha(__file__), module_sha256=sha(ROOT / "global_snapshot.inc"), commands=commands,
        engine_sha256=sha(output / "libcrprobe.so"), training_admissible=False, gates_closed=False,
        artifacts={str(path.relative_to(output)): sha(path) for path in output.rglob("*") if path.is_file()})
    (output / "build-manifest.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(dict(output=str(output), engine_sha256=record["engine_sha256"], manifest_sha256=sha(output / "build-manifest.json"))))


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    build()
