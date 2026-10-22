import hashlib
import json
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
SOURCE = WORKSPACE / "firstlight_CR"


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("native patch anchor differs: " + before[:100])
    return source.replace(before, after, 1)


def main():
    frozen = SOURCE / "artifacts/standalone-resident-20260927"
    output = ROOT / f"phase-snapshot-engine-{time.time_ns()}"
    output.mkdir()
    inputs = {}

    def read(path):
        content = path.read_bytes()
        inputs[str(path.relative_to(WORKSPACE))] = hashlib.sha256(content).hexdigest()
        return content

    probe = output / "probe"
    probe.mkdir()
    for path in sorted((frozen / "frozen/probe").iterdir()):
        if path.is_file():
            (probe / path.name).write_bytes(read(path))
    for name in ("battle_probe.c", "call_sret.S", "crash_trace.h"):
        (output / name).write_bytes(read(frozen / "frozen" / name))
    for name in ("resident_export.inc", "stage_commands.inc"):
        (output / name).write_bytes(read(frozen / name))
    for name in ("resident_multimatch.inc", "resident_belief_draw.inc", "fair_final_phase_snapshot.inc",
            "fair_final_phase_observation.h"):
        (probe / name).write_bytes(read(SOURCE / "native_runner/probe" / name))
    compact_source = read(WORKSPACE / ".tmp_remote_edit/compact_patch/resident_multimatch.inc").decode()
    compact_helpers = compact_source[compact_source.index("constexpr char kResidentBase64Alphabet"):
        compact_source.index("void resident_write_receipt(")]
    with (probe / "resident_multimatch.inc").open("a") as handle:
        handle.write("\n" + compact_helpers)
    phase_source = read(frozen / "build/phase_runtime_telemetry.inc").decode()
    if "cache_native_id" not in phase_source:
        raise ValueError("native-ID-aware phase cache required")
    phase_source = replace_once(phase_source, '#include "phase_runtime_layout.h"',
        '#include "phase_runtime_layout.h"\n#include "fair_final_phase_observation.h"')
    phase_source = replace_once(phase_source,
        "    const std::int32_t effective_speed = g_original_phase_effective_speed(const_cast<void *>(object));",
        "    const std::int32_t effective_speed = [&]() {\n"
        "      const FairFinalPhaseObservationScope observation_scope;\n"
        "      return g_original_phase_effective_speed(const_cast<void *>(object));\n    }();")
    phase_source = replace_once(phase_source,
        "  const std::int32_t result = g_original_phase_movement_scale(component, input);",
        "  const std::int32_t result = g_original_phase_movement_scale(component, input);\n"
        "  if (g_fair_final_phase_observation_depth != 0) return result;")
    (probe / "phase_runtime_telemetry.inc").write_text(phase_source)
    source = read(WORKSPACE / ".tmp_remote_edit/compact_patch/resident.cpp").decode()
    source = replace_once(source, "struct StoredSnapshot {",
        "struct FairFinalPhaseSnapshot;\n"
        "std::shared_ptr<const FairFinalPhaseSnapshot> capture_fair_final_phase_snapshot(\n"
        "    void *, std::uint64_t, std::uint64_t, std::int32_t);\n"
        "bool restore_fair_final_phase_snapshot(void *, std::uint64_t, std::uint64_t, std::int32_t,\n"
        "    const std::shared_ptr<const FairFinalPhaseSnapshot> &);\n\n"
        "struct StoredSnapshot {\n  std::shared_ptr<const FairFinalPhaseSnapshot> phase_snapshot;")
    source = replace_once(source,
        "    const bool captured = create_state_snapshot(manager, &wrapper, &metadata.digest, &metadata.serialized_bytes);",
        "    metadata.phase_snapshot = capture_fair_final_phase_snapshot(\n"
        "        manager, metadata.generation, metadata.state_epoch, metadata.tick);\n"
        "    const bool captured = metadata.phase_snapshot &&\n"
        "        create_state_snapshot(manager, &wrapper, &metadata.digest, &metadata.serialized_bytes);")
    source = replace_once(source,
        "    const bool verified = valid_prelude && verified_snapshot && result_readable && restored_tick == saved.tick &&",
        "    bool verified = valid_prelude && verified_snapshot && result_readable && restored_tick == saved.tick &&")
    for generation, epoch in [("g_control_generation", "g_state_epoch"), ("saved.generation", "g_native_render_state_epoch")]:
        anchor = f"      begin_runtime_telemetry_epoch(manager, {generation}, {epoch});"
        source = replace_once(source, anchor, anchor + "\n"
            f"      verified = verified && restore_fair_final_phase_snapshot(manager, {generation}, {epoch},\n"
            "          restored_tick, saved.phase_snapshot);")
    source = replace_once(source, '#include "phase_runtime_telemetry.inc"',
        '#include "phase_runtime_telemetry.inc"\n#include "fair_final_phase_snapshot.inc"')
    (output / "resident.cpp").write_text(source)
    draw = (probe / "resident_belief_draw.inc").read_text()
    anchor = "  for (int owner = 0; owner < 2; ++owner) {\n    std::memcpy(players[owner].hand.data"
    draw = replace_once(draw, anchor,
        "  const auto phase_snapshot = capture_fair_final_phase_snapshot(slot.manager, slot.generation, slot.state_epoch, tick);\n"
        "  if (!phase_snapshot) {\n"
        '    send_control_error(socket_fd, "local phase snapshot capture failed");\n'
        "    return true;\n  }\n" + anchor)
    anchor = "  begin_runtime_telemetry_epoch(slot.manager, slot.generation, g_state_epoch);"
    draw = replace_once(draw, anchor, anchor + "\n"
        "  if (!restore_fair_final_phase_snapshot(slot.manager, slot.generation, g_state_epoch, tick, phase_snapshot)) {\n"
        "    resident_save_bound_slot();\n"
        '    send_control_error(socket_fd, "local phase snapshot restore failed");\n'
        "    return true;\n  }")
    (probe / "resident_belief_draw.inc").write_text(draw)
    compiler_dir = Path.home() / "Library/Android/sdk/ndk/27.3.13750724/toolchains/llvm/prebuilt/darwin-x86_64/bin"
    common = ["-O2", "-fPIC", "-Wall", "-Wextra", "-Werror"]
    commands = [
        [str(compiler_dir / "aarch64-linux-android30-clang"), *common, "-c", str(output / "battle_probe.c"),
            "-o", str(output / "battle_probe.o")],
        [str(compiler_dir / "aarch64-linux-android30-clang++"), "-std=c++17", *common,
            "-DCR_STANDALONE_HOST", "-fvisibility=hidden", "-shared", "-Wl,-z,max-page-size=16384",
            "-I", str(probe), "-I", str(output), str(output / "resident.cpp"), str(output / "battle_probe.o"),
            str(output / "call_sret.S"), str(probe / "native_touch_interceptor.cpp"),
            str(probe / "native_call_arm64.S"), str(probe / "remaining_runtime_arm64.S"),
            "-llog", "-ldl", "-lz", "-o", str(output / "libcrprobe.so")],
    ]
    (output / "build-inputs.json").write_text(json.dumps(dict(inputs=inputs, commands=commands), indent=2))
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        with (output / "build.log").open("a") as handle:
            handle.write(result.stdout + result.stderr)
        if result.returncode:
            print(result.stdout + result.stderr, flush=True)
            raise RuntimeError("isolated native build failed: " + str(output))
    manifest = dict(schema="fair-final-phase-snapshot-build.v1", training_admissible=False,
        builder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), inputs=inputs, commands=commands,
        scope="single-slot-phase-snapshot-and-local-draw",
        exclusions=["multi-slot-telemetry", "Full128-search", "training-admission"],
        artifacts={str(path.relative_to(output)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in output.rglob("*") if path.is_file()})
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(dict(output=str(output), engine_sha256=manifest["artifacts"]["libcrprobe.so"])), flush=True)


if __name__ == "__main__":
    main()
