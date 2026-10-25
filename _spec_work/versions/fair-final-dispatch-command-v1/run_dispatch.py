import argparse
import hashlib
import json
from pathlib import Path
import types

from dispatch_audit import ROOT, generated_sources, validate_dispatch, validate_targets, verifier_source
import run_live
import run_plugin
import plugin_protocol
from trace_protocol import sha


LIVE_SOURCES = run_live.sources()
PARENT_ROOT = ROOT.parent / "fair-final-plugin-runtime-v1"


def sources():
    return generated_sources(LIVE_SOURCES)


def configure():
    run_plugin.ROOT = ROOT
    run_plugin.runtime_sources = sources
    protocol = sources()["plugin_protocol.py"]
    exec(compile(protocol, "generated-dispatch-protocol", "exec"), plugin_protocol.__dict__)


def verify(args):
    targets = validate_targets(args.library)
    verifier = types.ModuleType("dispatch_parent_verifier")
    verifier.__file__ = str(PARENT_ROOT / "verify_plugin.py")
    source = verifier_source(Path(verifier.__file__).read_text())
    exec(compile(source, "generated-dispatch-verifier", "exec"), verifier.__dict__)
    result = verifier.verify(args.attempt, args.launch_sha, args.receipt_sha,
        ancestor=args.ancestor, build_attempt=args.build, snapshot_parent=args.snapshot_parent)
    archive = args.attempt / "archive/plugin"
    dispatch = validate_dispatch(json.loads((archive / "trace-summary.json").read_text()),
        json.loads((archive / "report.json").read_text()))
    result.update(schema="FairFinalDispatchCommandVerification/v1", dispatch_command=dispatch,
        targets=targets, dispatch_runner_sha256=sha(__file__),
        dispatch_auditor_sha256=sha(ROOT / "dispatch_audit.py"),
        generated_parent_verifier_sha256=hashlib.sha256(source.encode()).hexdigest())
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    launch = subparsers.add_parser("launch")
    for name in ("build", "ancestor"):
        launch.add_argument("--" + name, type=Path, required=True)
        launch.add_argument("--" + name + "-launch-sha", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("attempt", type=Path)
    collect.add_argument("--launch-sha", required=True)
    verifier = subparsers.add_parser("verify")
    verifier.add_argument("attempt", type=Path)
    for name in ("launch-sha", "receipt-sha"):
        verifier.add_argument("--" + name, required=True)
    for name in ("ancestor", "build", "snapshot-parent", "output", "library"):
        verifier.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    configure()
    if args.operation == "launch":
        run_plugin.launch(args.build, args.build_launch_sha, args.ancestor,
            args.ancestor_launch_sha, arm=True, port=29789)
    elif args.operation == "collect":
        run_plugin.collect(args.attempt, args.launch_sha)
    else:
        verify(args)


if __name__ == "__main__":
    main()
