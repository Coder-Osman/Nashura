import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile


ROOT = Path(__file__).resolve().parent
INTEGRATION = "_spec_work/versions/fair-final-migration-integration-v1/"
SERIES = "_spec_work/versions/fair-final-migration-series-v1/"
SELECTION = "_spec_work/versions/fair-final-migration-selection-v1/"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_parent(parent, *, expected_launch_sha):
    parent = Path(parent)
    launch_bytes = (parent / "launch.json").read_bytes()
    if sha(launch_bytes) != expected_launch_sha:
        raise ValueError("pinned training launch differs")
    launch = json.loads(launch_bytes)
    if launch["training_admissible"] is not False or launch["formal_g2_pass"] is not False:
        raise ValueError("engineering training scope required")
    package = (parent / "payload.tar.gz").read_bytes()
    if sha(package) != launch["payload_sha256"]:
        raise ValueError("frozen training payload differs")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = Path(member.name)
            if (not member.isfile() or relative.is_absolute() or ".." in relative.parts
                    or str(relative) != member.name or member.name in files):
                raise ValueError("noncanonical or duplicate training payload member")
            files[member.name] = archive.extractfile(member).read()
    original_identity = json.loads(files["deployment.json"])
    if (original_identity != launch["source_sha256"]
            or set(files) != set(original_identity) | {"deployment.json"}
            or any(sha(files[name]) != expected for name, expected in original_identity.items())):
        raise ValueError("training deployment manifest differs")
    return launch, files, original_identity


def build_collection(parent, *, expected_launch_sha, runtime_parent=None,
        expected_runtime_launch_sha=None):
    launch, files, original_identity = read_parent(parent, expected_launch_sha=expected_launch_sha)
    return build_from_parent(launch, files, original_identity,
        expected_launch_sha=expected_launch_sha, runtime_parent=runtime_parent,
        expected_runtime_launch_sha=expected_runtime_launch_sha)


def build_selected_collection(parent, *, execution_sha, completion_sha, data_roots):
    active = ROOT.parent / "fair-final-active-migration-v1"
    for directory in (ROOT, active, ROOT.parent / "fair-final-migration-selection-v1",
            ROOT.parent / "fair-final-migration-series-v1", ROOT.parents[2] / "firstlight_CR"):
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
    from verify_selection_execution import verify
    from run_selection import checked_handoff, payload_files

    parent = Path(parent)
    proof = verify(parent, execution_sha=execution_sha, completion_sha=completion_sha,
        data_roots=data_roots)
    if (proof["status"] != "passed_independent_terminal_selection_content_scope"
            or proof["frozen_deployment_verified"] is not True
            or proof["execution_sha256"] != execution_sha
            or proof["completion_sha256"] != completion_sha
            or any(proof[name] is not False for name in
                ("training_admissible", "formal_g2_pass", "gates_closed"))):
        raise ValueError("independently verified engineering terminal selection required")
    execution_bytes = (parent / "execution.json").read_bytes()
    completion_bytes = (parent / "completion.json").read_bytes()
    if sha(execution_bytes) != execution_sha or sha(completion_bytes) != completion_sha:
        raise ValueError("selection evidence changed during collection handoff")
    execution, completion = json.loads(execution_bytes), json.loads(completion_bytes)
    checkpoint_sha = sha((parent / "selected.pt").read_bytes())
    if checkpoint_sha != completion["artifacts"]["selected.pt"]["sha256"]:
        raise ValueError("selected checkpoint changed during collection handoff")
    request = checked_handoff(parent / "inputs", execution["handoff_file_sha256"])
    files, identity = payload_files(parent / "inputs", request)
    launch = dict(payload_sha256=request["files"]["payload.tar.gz"]["sha256"], deterministic=True)
    package, report = build_from_parent(launch, files, identity,
        expected_launch_sha=request["parent_launch_sha256"])
    report.update(schema="FairFinalSelectedCollectionPayload/v1",
        selection_parent=dict(execution_sha256=execution_sha, completion_sha256=completion_sha,
            checkpoint_sha256=checkpoint_sha,
            verification=proof), native_collection_executed=False)
    return package, report


def build_from_parent(launch, files, original_identity, *, expected_launch_sha,
        runtime_parent=None, expected_runtime_launch_sha=None):
    if (runtime_parent is None) != (expected_runtime_launch_sha is None):
        raise ValueError("runtime parent and pinned launch SHA required together")
    runtime = SERIES + "deterministic_runtime.py"
    deterministic = launch.get("deterministic", False)
    runtime_evidence = None
    if runtime_parent is not None:
        ancestor, ancestor_files, _ = read_parent(runtime_parent,
            expected_launch_sha=expected_runtime_launch_sha)
        if ("deterministic" in launch or runtime not in files
                or ancestor.get("deterministic") is not True
                or launch.get("parent_launch_sha256") != expected_runtime_launch_sha
                or ancestor_files.get(runtime) != files[runtime]
                or b"from deterministic_runtime import configure\n" not in files[SELECTION + "train_round.py"]
                or b"print(json.dumps(configure()), flush=True)" not in files[SELECTION + "train_round.py"]):
            raise ValueError("explicit inherited runtime identity differs")
        deterministic = True
        runtime_evidence = dict(parent_launch_sha256=expected_runtime_launch_sha,
            parent_payload_sha256=ancestor["payload_sha256"], runtime_sha256=sha(files[runtime]))
    if type(deterministic) is not bool or deterministic != (runtime in files):
        raise ValueError("training runtime identity differs")
    collector = (ROOT / "collect_selected.py").read_text()
    if deterministic:
        anchor = "    teacher, _, _ = load_checkpoint(args.source, device=args.device,"
        if collector.count(anchor) != 1:
            raise ValueError("collection runtime integration anchor differs")
        collector = collector.replace(anchor,
            "    from deterministic_runtime import configure\n"
            "    runtime_evidence = configure()\n"
            "    print(json.dumps(runtime_evidence), flush=True)\n" + anchor, 1)
        anchor = "    private.update(source_sha256_manifest=source_manifest,"
        if collector.count(anchor) != 1:
            raise ValueError("collection audit integration anchor differs")
        collector = collector.replace(anchor,
            "    private.update(deterministic_runtime=runtime_evidence,\n"
            "        deterministic_runtime_sha=hashlib.sha256(\n"
            "            (ROOT.parent / 'fair-final-migration-series-v1/deterministic_runtime.py').read_bytes()).hexdigest())\n"
            + anchor, 1)
    updates = {INTEGRATION + "collect_selected.py": collector.encode()}
    for name in ("seed_registration.py", "seed_sources.py", "selected_collection_worker.py"):
        updates[INTEGRATION + name] = (ROOT / name).read_bytes()
    launcher = ROOT.parents[1] / "evidence/fair-final-v1-20261019/phase_snapshot_launcher.py"
    updates["phase_snapshot_launcher.py"] = launcher.read_bytes()
    changes = {name: dict(before=original_identity.get(name), after=sha(data))
        for name, data in updates.items() if files.get(name) != data}
    files.update(updates)
    identity = {name: sha(data) for name, data in files.items() if name != "deployment.json"}
    for name in original_identity:
        if name not in updates and identity[name] != original_identity[name]:
            raise ValueError("collection changed frozen training source")
    for name, data in updates.items():
        compile(data, name, "exec")
    files["deployment.json"] = json.dumps(identity, sort_keys=True).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    package = gzip.compress(buffer.getvalue(), mtime=0)
    report = dict(schema="FairFinalCollectionPayload/v1", parent_launch_sha256=expected_launch_sha,
        parent_payload_sha256=launch["payload_sha256"], payload_sha256=sha(package),
        builder_sha256=sha(Path(__file__).read_bytes()),
        source_sha256=identity, changes=changes, deterministic=deterministic,
        inherited_runtime_evidence=runtime_evidence,
        training_sources_preserved=True,
        requires_terminal_selected_parent=True, requires_registered_tasks=True,
        native_collection_executed=False,
        training_admissible=False, formal_g2_pass=False, gates_closed=False)
    return package, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("parent", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--parent-launch-sha")
    mode.add_argument("--selection-execution-sha")
    parser.add_argument("--selection-completion-sha")
    parser.add_argument("--selection-data-roots", type=Path, nargs="+")
    parser.add_argument("--runtime-parent", type=Path)
    parser.add_argument("--runtime-parent-launch-sha")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.selection_execution_sha:
        if (not args.selection_completion_sha or not args.selection_data_roots
                or args.runtime_parent is not None or args.runtime_parent_launch_sha is not None):
            parser.error("selection requires completion SHA and data roots, without runtime overrides")
        package, report = build_selected_collection(args.parent,
            execution_sha=args.selection_execution_sha, completion_sha=args.selection_completion_sha,
            data_roots=args.selection_data_roots)
    else:
        if args.selection_completion_sha is not None or args.selection_data_roots is not None:
            parser.error("selection evidence requires selection execution mode")
        package, report = build_collection(args.parent, expected_launch_sha=args.parent_launch_sha,
            runtime_parent=args.runtime_parent, expected_runtime_launch_sha=args.runtime_parent_launch_sha)
    args.output.mkdir(exist_ok=False)
    (args.output / "payload.tar.gz").write_bytes(package)
    (args.output / "collection-payload.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
