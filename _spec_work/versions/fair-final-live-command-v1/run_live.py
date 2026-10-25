import argparse
import json
from pathlib import Path

from live_audit import EXTRA_TARGETS, ROOT, SCOPE, generated_sources, validate_live
import run_plugin
import plugin_protocol
from trace_protocol import sha
from target_identity import validate_targets


PARENT_ROOT = ROOT.parent / "fair-final-plugin-runtime-v1"
PARENT_SUMMARIZE = plugin_protocol.summarize


def sources():
    files = run_plugin.upload_sources()
    files["verify_trace.py"] = (ROOT.parent / "fair-final-random-execution-v1/verify_trace.py").read_bytes()
    for name in ("plugin_launcher.py", "plugin_probe.py", "plugin_worker.py", "plugin_protocol.py",
            "plugin_audit.py", "plugin_seal.py", "random_entries.c"):
        files[name] = (PARENT_ROOT / name).read_bytes()
    return generated_sources(files)


def configure():
    run_plugin.ROOT = ROOT
    run_plugin.runtime_sources = sources
    plugin_protocol.TARGETS.update(EXTRA_TARGETS)
    plugin_protocol.summarize = summarize


def summarize(*args, **kwargs):
    result = PARENT_SUMMARIZE(*args, **kwargs)
    result["scope"] = SCOPE
    return result


def verify(args):
    import verify_plugin

    targets = validate_targets(args.library)
    result = verify_plugin.verify(args.attempt, args.launch_sha, args.receipt_sha,
        ancestor=args.ancestor, build_attempt=args.build, snapshot_parent=args.snapshot_parent)
    archive = args.attempt / "archive/plugin"
    live = validate_live(json.loads((archive / "trace-summary.json").read_text()),
        json.loads((archive / "report.json").read_text()))
    result.update(schema="FairFinalLiveCommandVerification/v1", live_command=live,
        targets=targets, live_verifier_sha256=sha(__file__),
        live_auditor_sha256=sha(ROOT / "live_audit.py"),
        target_auditor_sha256=sha(ROOT / "target_identity.py"))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    launch = subparsers.add_parser("launch")
    launch.add_argument("--build", type=Path, required=True)
    launch.add_argument("--build-launch-sha", required=True)
    launch.add_argument("--ancestor", type=Path, required=True)
    launch.add_argument("--ancestor-launch-sha", required=True)
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
