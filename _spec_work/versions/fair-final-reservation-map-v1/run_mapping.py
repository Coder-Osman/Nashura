import argparse
import hashlib
import json
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-dispatch-command-v1"))
import run_dispatch
from reservation_audit import validate_mapping


def sources():
    files = run_dispatch.sources()
    files["plugin_probe.py"] = (ROOT / "plugin_probe.py").read_bytes()
    return files


def configure():
    run_dispatch.configure()
    run_dispatch.run_plugin.ROOT = ROOT
    run_dispatch.run_plugin.runtime_sources = sources


def verify(args):
    targets = run_dispatch.validate_targets(args.library)
    verifier = types.ModuleType("reservation_parent_verifier")
    verifier.__file__ = str(run_dispatch.PARENT_ROOT / "verify_plugin.py")
    source = run_dispatch.verifier_source(Path(verifier.__file__).read_text())
    exec(compile(source, "generated-reservation-parent-verifier", "exec"), verifier.__dict__)
    result = verifier.verify(args.attempt, args.launch_sha, args.receipt_sha,
        ancestor=args.ancestor, build_attempt=args.build, snapshot_parent=args.snapshot_parent)
    archive = args.attempt / "archive/plugin"
    read = lambda name: json.loads((archive / name).read_text())
    summary, report = read("trace-summary.json"), read("report.json")
    dispatch = run_dispatch.validate_dispatch(summary, report)
    mapping = validate_mapping(summary, report, read("measurements.json"))
    result.update(schema="FairFinalReservationMappingVerification/v1", targets=targets,
        dispatch_command=dispatch, reservation_mapping=mapping,
        mapping_runner_sha256=run_dispatch.sha(__file__),
        mapping_auditor_sha256=run_dispatch.sha(ROOT / "reservation_audit.py"),
        generated_parent_verifier_sha256=hashlib.sha256(source.encode()).hexdigest())
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="operation", required=True)
    launch = commands.add_parser("launch")
    for name in ("build", "ancestor"):
        launch.add_argument("--" + name, type=Path, required=True)
        launch.add_argument("--" + name + "-launch-sha", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("attempt", type=Path)
    collect.add_argument("--launch-sha", required=True)
    check = commands.add_parser("verify")
    check.add_argument("attempt", type=Path)
    for name in ("launch-sha", "receipt-sha"):
        check.add_argument("--" + name, required=True)
    for name in ("ancestor", "build", "snapshot-parent", "output", "library"):
        check.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    configure()
    if args.operation == "launch":
        run_dispatch.run_plugin.launch(args.build, args.build_launch_sha,
            args.ancestor, args.ancestor_launch_sha, arm=True, port=29789)
    elif args.operation == "collect":
        run_dispatch.run_plugin.collect(args.attempt, args.launch_sha)
    else:
        verify(args)


if __name__ == "__main__":
    main()
