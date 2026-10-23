import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import resource

from trace_protocol import LOG_FLAGS, filter_spec, sha


def bound_output_files():
    maximum = 256 * 1024**2
    _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    if hard != resource.RLIM_INFINITY and hard < maximum:
        raise ValueError("trace inherited output file limit is too small")
    resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))


def service_source(path):
    source = Path(path).read_text()
    original = "deadline = time.monotonic()+140"
    if source.count(original) != 1:
        raise ValueError("trace service startup deadline differs")
    updated = source.replace(original, "deadline = time.monotonic()+600")
    ast.parse(updated)
    return updated, hashlib.sha256(updated.encode()).hexdigest()


def command_with_trace(arguments, *, probe, engine_sha, baseline, settings, output):
    arguments = list(arguments)
    if sha(probe) != engine_sha or arguments.count(baseline) != 1:
        raise ValueError("trace engine or baseline command differs")
    if sha(arguments[0]) != settings["qemu_sha256"]:
        raise ValueError("QEMU identity differs")
    if any(flag in arguments for flag in ("-d", "-D", "-dfilter", "-one-insn-per-tb", "-singlestep")):
        raise ValueError("preexisting QEMU logging flags")
    arguments[arguments.index(baseline)] = str(Path(probe).resolve())
    indices = [index for index, value in enumerate(arguments) if value.startswith("LD_LIBRARY_PATH=")]
    if len(indices) != 1:
        raise ValueError("trace library search paths differ")
    index = indices[0]
    arguments[index] = "LD_LIBRARY_PATH=" + str(Path(probe).resolve().parent) + ":" + arguments[index].split("=", 1)[1]
    if settings["mode"] == "trace":
        arguments[1:1] = ["-d", LOG_FLAGS, "-dfilter", filter_spec(settings["center_base"])["expression"],
            "-D", str(output)]
    elif settings["mode"] != "calibration":
        raise ValueError("unknown execution trace mode")
    return arguments


def main():
    parser = argparse.ArgumentParser()
    for name in ("project", "instance", "probe", "trace-settings"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--engine-sha256", required=True)
    args = parser.parse_args()
    settings = json.loads(args.trace_settings.read_text())
    source = args.project / "research/standalone-resident-p18-stepatomic/service.py"
    if sha(source) != settings["service_sha256"]:
        raise ValueError("trace base service differs")
    specification = importlib.util.spec_from_file_location("random_execution_service", source)
    service = importlib.util.module_from_spec(specification)
    updated, updated_sha = service_source(source)
    exec(compile(updated, str(source), "exec"), service.__dict__)
    original = service.command

    def command(port, instance):
        arguments, identity = original(port, instance)
        arguments = command_with_trace(arguments, probe=args.probe, engine_sha=args.engine_sha256,
            baseline=str(service.ROOT / "libresident.so"), settings=settings,
            output=args.instance.parent / "qemu.log")
        identity["random_execution_trace"] = dict(settings_sha256=sha(args.trace_settings),
            launcher_sha256=sha(__file__), protocol_sha256=sha(Path(__file__).with_name("trace_protocol.py")),
            startup_deadline_seconds=600, startup_service_sha256=updated_sha)
        identity["phase_snapshot_engine"] = dict(path=str(args.probe.resolve()), sha256=args.engine_sha256,
            launcher_sha256=sha(__file__), service_sha256=sha(source))
        return arguments, identity

    service.command = command
    args.instance.mkdir(parents=True, exist_ok=False)
    bound_output_files()
    service.serve(args.port, args.instance)


if __name__ == "__main__":
    main()
