import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
INTEGRATION = ROOT.parent / "fair-final-migration-integration-v1"
sys.path[:0] = [str(INTEGRATION), str(ROOT.parents[2] / "firstlight_CR")]

from collect_selected import argument_parser
from seed_registration import validate_registration
from selected_collection_worker import validate_request
from verify_selected_collection import deployment_identity
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


def file_identity(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular pinned input required: " + str(path))
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return dict(sha256=digest.hexdigest(), bytes=size)


def read_json(path):
    return json.loads(Path(path).read_bytes())


def parse_request(path, expected_sha, deployment_root):
    if file_identity(path)["sha256"] != require_sha(expected_sha):
        raise ValueError("pinned round request differs")
    request = read_json(path)
    arguments = request.get("collector_args")
    if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
        raise ValueError("explicit collector arguments required")
    flags = [item for item in arguments if item.startswith("--")]
    if len(flags) != len(set(flags)) or any("=" in flag for flag in flags):
        raise ValueError("duplicate or noncanonical collector option")
    parser = argument_parser()
    parser.allow_abbrev = False
    options = parser.parse_args(arguments)
    root = options.output.resolve().parent
    if options.output.resolve() != root / "data" or options.attempt_id != 1:
        raise ValueError("new round requires first attempts and worker-owned data roots")
    validate_request(request, root=root, deployment_root=deployment_root)
    common = {key: value for key, value in vars(options).items()
        if key not in {"match_id", "output", "port", "attempt_id"}}
    common["engine"] = request["engine"]
    return request, options, root, common


def pinned_inputs(options, request, paths, deployment_root, identity):
    files = {Path(path).absolute() for path in paths}
    for name in ("private_plan", "seed_registration", "seed_source_map", "source",
            "selected", "loss_weights", "selection_config"):
        files.add(getattr(options, name).absolute())
    for name in ("excluded_seed_inventories", "seed_source_artifacts"):
        files.update(path.absolute() for path in getattr(options, name))
    files.add(deployment_root / "deployment.json")
    files.update(deployment_root / name for name in identity)
    engine = request["engine"]
    files.update((Path(engine["probe"]).absolute(),
        Path(engine["project"]).absolute() / "research/standalone-resident-p18-stepatomic/service.py"))
    for parent in options.parent_data_roots:
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("complete regular parent data roots required")
        for path in parent.rglob("*"):
            if path.is_symlink():
                raise ValueError("parent history contains symbolic link")
            if path.is_file():
                files.add(path.absolute())
    return {str(path): file_identity(path) for path in sorted(files)}


def check_preflight(report, options, seed_evidence):
    expected = dict(schema="FairFinalSelectedCollectionPreflight/v1",
        status="passed_preflight_only", match_id=options.match_id,
        selected_checkpoint_sha=options.selected_sha, plan_sha=options.plan_sha,
        training_admissible=False, formal_g2_pass=False, **seed_evidence)
    if any(key not in report or semantic_digest(report[key]) != semantic_digest(value)
            for key, value in expected.items()):
        raise ValueError("collector preflight identity or engineering scope differs")
    require_sha(report["selected_network_sha"])


def prepare(request_paths, *, request_shas, deployment_root, deployment_sha,
        output, preflight_timeout=3600):
    if (not request_paths or len(request_paths) != len(request_shas)
            or type(preflight_timeout) is not int or not 1 <= preflight_timeout <= 14400):
        raise ValueError("nonempty pinned requests and bounded preflight required")
    deployment_root, output = Path(deployment_root).resolve(), Path(output).absolute()
    paths = [Path(path).absolute() for path in request_paths]
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate request input")
    identity = deployment_identity(deployment_root, require_sha(deployment_sha))
    entries = [parse_request(path, digest, deployment_root)
        for path, digest in zip(paths, request_shas)]
    options = entries[0][1]
    if any(entry[3] != entries[0][3] for entry in entries[1:]):
        raise ValueError("round requests mix lineage, registration or engine inputs")
    plan = read_json(options.private_plan)
    matches = plan["rounds"][-1]["manifest"]["matches"]
    tasks = [entry[1].match_id for entry in entries]
    if len(tasks) != len(set(tasks)) or set(tasks) != set(matches):
        raise ValueError("exact complete registered task coverage required")
    registration = read_json(options.seed_registration)
    seed_evidence = validate_registration(registration,
        expected_sha=options.seed_registration_sha, plan=plan, expected_plan_sha=options.plan_sha,
        inventory_paths=options.excluded_seed_inventories, source_map_path=options.seed_source_map,
        source_paths=options.seed_source_artifacts, match_id=tasks[0])
    if len(plan["rounds"]) not in (2, 3) or len(options.parent_data_roots) != len(plan["rounds"]) - 1:
        raise ValueError("complete supplementary parent history required")
    roots = [entry[2] for entry in entries]
    if len(set(roots)) != len(roots) or any(root.exists() for root in roots):
        raise ValueError("distinct unused collection roots required")
    if any(left != right and (left.is_relative_to(right) or right.is_relative_to(left))
            for left in roots for right in roots):
        raise ValueError("collection roots overlap")
    snapshot = pinned_inputs(options, entries[0][0], paths, deployment_root, identity)
    expected_source = plan["rounds"][-1]["manifest"]["identity"]["source_a_sha"]
    if (file_identity(options.source)["sha256"] != expected_source
            or file_identity(options.selected)["sha256"] != options.selected_sha):
        raise ValueError("source or selected checkpoint identity differs")
    protected = [deployment_root, *[path.resolve() for path in options.parent_data_roots], *roots]
    if (any(output.resolve().is_relative_to(path) or path.is_relative_to(output.resolve())
            for path in protected)
            or any(Path(path).resolve().is_relative_to(output.resolve()) for path in snapshot)):
        raise ValueError("coordinator output overlaps frozen or collection inputs")
    for root in roots:
        if any(Path(path).resolve().is_relative_to(root) for path in snapshot):
            raise ValueError("collection output contains pinned input")
    output.mkdir(parents=True, exist_ok=False)
    try:
        results = []
        network_sha = None
        for index, (request, current, root, _) in enumerate(entries):
            command = [sys.executable, "-B", "-u", str(deployment_root /
                "_spec_work/versions/fair-final-migration-integration-v1/collect_selected.py"),
                *request["collector_args"], "--preflight-only"]
            log = output / (str(index) + "-preflight.log")
            with log.open("xb") as stream:
                subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                    cwd=deployment_root, timeout=preflight_timeout, check=True)
            report = json.loads(log.read_text().splitlines()[-1])
            check_preflight(report, current, seed_evidence)
            if network_sha is not None and report["selected_network_sha"] != network_sha:
                raise ValueError("round preflights disagree on selected network")
            network_sha = report["selected_network_sha"]
            if snapshot != pinned_inputs(options, entries[0][0], paths, deployment_root, identity):
                raise ValueError("frozen round inputs changed during preflight")
            if any(path.exists() for path in roots):
                raise ValueError("preflight created collection output")
            results.append(dict(match_id=current.match_id, root=str(root),
                request_sha256=request_shas[index], preflight=report,
                log_identity=file_identity(log), worker_command=[sys.executable, "-B", "-u",
                    str(deployment_root / "_spec_work/versions/fair-final-migration-integration-v1/selected_collection_worker.py"),
                    "--root", str(root), "--request", str(paths[index]), "--request-sha", request_shas[index],
                    "--deployment-root", str(deployment_root), "--deployment-sha", deployment_sha]))
        result = dict(schema="FairFinalCollectionRoundPreflight/v1", status="prepared_only",
            round=registration["registered_round"], plan_sha=options.plan_sha,
            deployment_sha256=deployment_sha, coordinator_sha256=file_identity(__file__)["sha256"],
            selected_network_sha=network_sha, inputs=snapshot, tasks=results,
            native_collection_executed=False, training_admissible=False,
            formal_g2_pass=False, gates_closed=False)
        durable_write(output / "prepared.json", json_bytes(result))
        return result
    except BaseException as error:
        durable_write(output / "failure.json", json_bytes(dict(error=repr(error),
            training_admissible=False, formal_g2_pass=False, gates_closed=False)))
        raise


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--engineering-acceptance", required=True, action="store_true")
    parser.add_argument("--requests", nargs="+", type=Path, required=True)
    parser.add_argument("--request-shas", nargs="+", required=True)
    parser.add_argument("--deployment-root", type=Path, required=True)
    parser.add_argument("--deployment-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-timeout", type=int, default=3600)
    options = vars(parser.parse_args())
    options.pop("engineering_acceptance")
    options["request_paths"] = options.pop("requests")
    result = prepare(**options)
    print(json.dumps(dict(status=result["status"], tasks=len(result["tasks"]),
        training_admissible=False, formal_g2_pass=False)), flush=True)


if __name__ == "__main__":
    main()
