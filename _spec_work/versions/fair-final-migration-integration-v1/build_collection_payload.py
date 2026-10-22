import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
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
    for name in ("seed_registration.py", "seed_sources.py"):
        updates[INTEGRATION + name] = (ROOT / name).read_bytes()
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
    parser.add_argument("--parent-launch-sha", required=True)
    parser.add_argument("--runtime-parent", type=Path)
    parser.add_argument("--runtime-parent-launch-sha")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package, report = build_collection(args.parent, expected_launch_sha=args.parent_launch_sha,
        runtime_parent=args.runtime_parent, expected_runtime_launch_sha=args.runtime_parent_launch_sha)
    args.output.mkdir(exist_ok=False)
    (args.output / "payload.tar.gz").write_bytes(package)
    (args.output / "collection-payload.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
