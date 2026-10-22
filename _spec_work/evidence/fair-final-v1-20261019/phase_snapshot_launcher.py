import argparse
import hashlib
import importlib.util
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--instance", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--engine-sha256", required=True)
    args = parser.parse_args()
    source = args.project/"research/standalone-resident-p18-stepatomic/service.py"
    specification = importlib.util.spec_from_file_location("fair_final_phase_engine", source)
    service = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(service)
    original = service.command

    def command(port, instance):
        argv, identity = original(port, instance)
        digest = hashlib.sha256(args.probe.read_bytes()).hexdigest()
        if digest != args.engine_sha256:
            raise ValueError("isolated phase engine identity changed")
        baseline = str(service.ROOT/"libresident.so")
        if argv.count(baseline) != 1:
            raise ValueError("baseline engine command changed")
        argv[argv.index(baseline)] = str(args.probe.resolve())
        library_paths = [index for index, value in enumerate(argv) if value.startswith("LD_LIBRARY_PATH=")]
        if len(library_paths) != 1:
            raise ValueError("baseline library search paths changed")
        index = library_paths[0]
        argv[index] = "LD_LIBRARY_PATH="+str(args.probe.resolve().parent)+":"+argv[index].split("=", 1)[1]
        identity["phase_snapshot_engine"] = dict(path=str(args.probe.resolve()), sha256=digest,
            launcher_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            service_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        return argv, identity

    service.command = command
    args.instance.mkdir(parents=True, exist_ok=True)
    service.serve(args.port, args.instance)


if __name__ == "__main__":
    main()
