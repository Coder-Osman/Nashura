import argparse
import importlib.util
import json
from pathlib import Path

from plugin_protocol import filter_spec, sha
from trace_launcher import bound_output_files, command_with_trace


def plugin_arguments(arguments, settings, output):
    if settings["variant"] == "baseline":
        return arguments
    if sha(settings["plugin_qemu"]) != settings["plugin_qemu_sha256"]:
        raise ValueError("isolated QEMU binary identity differs")
    arguments[0] = settings["plugin_qemu"]
    if settings.get("reserved_va") is not None:
        if settings["reserved_va"] != 1024**4:
            raise ValueError("isolated reserved guest address space differs")
        arguments[1:1] = ["-R", hex(settings["reserved_va"])]
    if settings["variant"] == "upgraded":
        return arguments
    if settings["variant"] != "plugin" or sha(settings["plugin"]) != settings["plugin_sha256"]:
        raise ValueError("plugin execution configuration differs")
    if any("," in str(value) for value in (settings["plugin"], output)):
        raise ValueError("plugin paths contain QEMU option separator")
    if settings.get("arm_after_ready"):
        specification = f"{settings['plugin']},arm={output.parent}/arm-base.txt,output={output}"
    else:
        window = filter_spec(settings["center_base"])
        specification = (f"{settings['plugin']},first={window['first_base']:x},"
            f"last={window['last_base']:x},output={output}")
    arguments[1:1] = ["-plugin", specification]
    return arguments


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--instance", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text())
    source = Path(settings["base_project"]) / "research/standalone-resident-p18-stepatomic/service.py"
    if sha(source) != settings["service_sha256"]:
        raise ValueError("plugin service source identity differs")
    specification = importlib.util.spec_from_file_location("plugin_execution_service", source)
    service = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(service)
    original = service.command

    def command(port, instance):
        arguments, identity = original(port, instance)
        arguments = command_with_trace(arguments, probe=settings["probe"],
            engine_sha=settings["engine_sha256"], baseline=str(service.ROOT / "libresident.so"),
            settings=dict(mode="calibration", qemu_sha256=settings["qemu_sha256"]),
            output=args.instance.parent / "qemu.log")
        arguments = plugin_arguments(arguments, settings, args.instance.parent / "qemu.log")
        identity["random_plugin_trace"] = dict(variant=settings["variant"],
            settings_sha256=sha(args.settings), launcher_sha256=sha(__file__),
            qemu_sha256=sha(arguments[0]), plugin_sha256=settings["plugin_sha256"]
                if settings["variant"] == "plugin" else None)
        identity["phase_snapshot_engine"] = dict(path=settings["probe"],
            sha256=settings["engine_sha256"], launcher_sha256=sha(__file__),
            service_sha256=sha(source))
        return arguments, identity

    service.command = command
    args.instance.mkdir(parents=True, exist_ok=False)
    bound_output_files()
    service.serve(args.port, args.instance)


if __name__ == "__main__":
    main()
