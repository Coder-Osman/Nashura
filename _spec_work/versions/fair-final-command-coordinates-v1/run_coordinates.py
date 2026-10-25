import argparse
import hashlib
import json
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-reservation-map-v1"))
import run_mapping
from coordinate_audit import validate_coordinates, validate_targets
from dispatch_audit import replace_once


def sources():
    files = run_mapping.sources()
    replace = replace_once
    source = files["random_entries.c"].decode()
    source = replace(source, "registers[5]", "registers[6]")
    source = replace(source, '{"pc", "x0", "x1", "x30", "x8"}',
        '{"pc", "x0", "x1", "x30", "x8", "x2"}')
    source = replace(source, "register_index < 5", "register_index < 6", count=3)
    source = replace(source, "values[5] = {0}", "values[6] = {0}")
    source = replace(source, '"\\t%016" PRIx64 "\\t%016" PRIx64 "\\t%016" PRIx64 "\\t%016" PRIx64 "\\n", sequence, cpu_index,',
        '"\\t%016" PRIx64 "\\t%016" PRIx64 "\\t%016" PRIx64 "\\t%016" PRIx64 "\\t%016" PRIx64 "\\n", sequence, cpu_index,')
    source = replace(source, "expected_pc, values[0], values[1], values[2], values[3], values[4])",
        "expected_pc, values[0], values[1], values[2], values[3], values[4], values[5])")
    files["random_entries.c"] = replace(source, "FairFinalPluginEntries/v4", "FairFinalPluginEntries/v5").encode()
    protocol = files["plugin_protocol.py"].decode()
    protocol = protocol.replace("FairFinalPluginEntries/v4", "FairFinalPluginEntries/v5")
    protocol = replace(protocol, 'versions[b"FairFinalPluginEntries/v5\\n"] = 4',
        'versions[b"FairFinalPluginEntries/v5\\n"] = 5')
    protocol = protocol.replace("version == 4", "version == 5")
    protocol = replace(protocol, "versions[header] != 4", "versions[header] != 5")
    protocol = replace(protocol, "len(fields) != 9", "len(fields) != 10")
    protocol = replace(protocol, 'entry["target"] = values[5]',
        'entry["target"] = values[5]\n                    entry["coordinate_y"] = values[6]')
    protocol = replace(protocol, "FairFinalDispatchEntrySummary/v1", "FairFinalCoordinateEntrySummary/v1")
    files["plugin_protocol.py"] = protocol.encode()
    probe = files["plugin_probe.py"].decode()
    probe = replace(probe, "                before = position()\n                receipt = original(action, **kwargs)",
        '                placement = environment.action_mask(action.owner, environment.raw_observation).placement_masks.get(str(action.hand_index))\n'
        '                if placement is None:\n                    raise ValueError("coordinate placement missing")\n'
        '                context = dict(model_subcell_offset=placement.get("model_subcell_offset"))\n'
        "                before = position()\n                receipt = original(action, **kwargs)")
    probe = replace(probe, "request=kwargs, receipt=receipt, log_start=before, log_end=position()))",
        "request=kwargs, receipt=receipt, log_start=before, log_end=position(), coordinate_context=context))")
    files["plugin_probe.py"] = replace(probe, 'records=measurements, reservation_schema=',
        'records=measurements, coordinate_schema="FairFinalCommandCoordinates/v1", reservation_schema=').encode()
    return files


def configure():
    run_mapping.configure()
    run_mapping.run_dispatch.run_plugin.ROOT = ROOT
    run_mapping.run_dispatch.run_plugin.runtime_sources = sources
    exec(compile(sources()["plugin_protocol.py"], "generated-coordinate-protocol", "exec"),
        run_mapping.run_dispatch.plugin_protocol.__dict__)


def verify(args):
    targets = validate_targets(args.library)
    parent = run_mapping.run_dispatch
    verifier = types.ModuleType("coordinate_parent_verifier")
    verifier.__file__ = str(parent.PARENT_ROOT / "verify_plugin.py")
    source = parent.verifier_source(Path(verifier.__file__).read_text())
    source = replace_once(source, "FairFinalPluginEntries/v4", "FairFinalPluginEntries/v5")
    source = replace_once(source, "requires explicit v4 trace", "requires explicit v5 trace")
    exec(compile(source, "generated-coordinate-parent-verifier", "exec"), verifier.__dict__)
    result = verifier.verify(args.attempt, args.launch_sha, args.receipt_sha,
        ancestor=args.ancestor, build_attempt=args.build, snapshot_parent=args.snapshot_parent)
    archive = args.attempt / "archive/plugin"
    read = lambda name: json.loads((archive / name).read_text())
    summary, report, measurements = (read(name) for name in
        ("trace-summary.json", "report.json", "measurements.json"))
    dispatch_summary = dict(summary, schema="FairFinalDispatchEntrySummary/v1")
    result.update(schema="FairFinalCommandCoordinateVerification/v1", targets=targets,
        dispatch_command=parent.validate_dispatch(dispatch_summary, report),
        reservation_mapping=run_mapping.validate_mapping(summary, report, measurements),
        command_coordinates=validate_coordinates(summary, report, measurements),
        coordinate_runner_sha256=parent.sha(__file__),
        coordinate_auditor_sha256=parent.sha(ROOT / "coordinate_audit.py"),
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
        run_mapping.run_dispatch.run_plugin.launch(args.build, args.build_launch_sha,
            args.ancestor, args.ancestor_launch_sha, arm=True, port=29789)
    elif args.operation == "collect":
        run_mapping.run_dispatch.run_plugin.collect(args.attempt, args.launch_sha)
    else:
        verify(args)


if __name__ == "__main__":
    main()
