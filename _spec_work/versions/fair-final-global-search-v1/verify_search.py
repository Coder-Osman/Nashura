import argparse
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

from run_search import ROOT, PORTS, preparation, sha
from snapshot_audit import validate_records
from verify_native_general import (ANCHOR_SEED, SEARCH_SEED, SOURCE_SHA, EXCLUSIONS,
    FairHistory, FrozenDeckPrior, GeneralHistorySearchProvider, HistoryCycleCursor,
    IncrementalRefillBeliefFactory, SearchLimits, advance_completion, duration,
    equal_fields, payload, semantic_digest, split_seed, validate_layouts, validate_world)


def validate_audits(audits):
    if not isinstance(audits, list) or len(audits) < 2:
        raise ValueError("global search actual and belief audit transports missing")
    counts = {name: 0 for name in ("create", "restore", "release")}
    ports = set()
    for audit in audits:
        if set(audit) != {"port", "records", "summary"} or audit["port"] not in PORTS:
            raise ValueError("global search audit endpoint differs")
        summary = validate_records(audit["records"])
        if summary != audit["summary"]:
            raise ValueError("global search audit summary differs from records")
        ports.add(audit["port"])
        for name in counts:
            counts[name] += summary["counts"][name]
    if ports != set(PORTS) or counts["restore"] < 512:
        raise ValueError("global search complete restore coverage absent")
    return counts


def validate_spans(records, audits, searches):
    if len(searches) != len(records):
        raise ValueError("global search per-root snapshot spans missing")
    cursors = {}
    for row, search in zip(records, searches):
        if (set(search) != {"fair_sha", "tick", "spans"} or search["fair_sha"] != row["fair_sha"]
                or type(search["tick"]) is not int or search["tick"] != row["tick"]):
            raise ValueError("global search snapshot span root differs")
        root_restores = 0
        endpoints = set()
        for span in search["spans"]:
            if (set(span) != {"transport", "start", "end"}
                    or any(type(span[name]) is not int for name in span)
                    or not 0 <= span["transport"] < len(audits)
                    or span["transport"] in endpoints):
                raise ValueError("global search snapshot span endpoint differs")
            index = span["transport"]
            endpoints.add(index)
            audit = audits[index]
            if not cursors.get(index, 0) <= span["start"] < span["end"] <= len(audit["records"]):
                raise ValueError("global search snapshot span overlaps or escapes records")
            cursors[index] = span["end"]
            if audit["port"] == PORTS[1]:
                root_restores += sum(record["operation"] == "restore"
                    and record["digest"]["sha256"] == row["world_lifecycle"]["root_digest"]
                    for record in audit["records"][span["start"]:span["end"]])
        if root_restores < max(128, row["world_lifecycle"]["restores"]):
            raise ValueError("global search root lacks matching complete-digest restores")


def validate_report(report, histories, trace, prior, sources, engine_sha):
    equal_fields(report, dict(schema="FairFinalGlobalSnapshotSearchProbe/v1", status="passed",
        source_sha256=sources, checkpoint_sha256=SOURCE_SHA, tick=110, owner=0,
        limits=asdict(SearchLimits()), engine_sha256=engine_sha,
        general_provider_sha256=sha(ROOT.parent / "fair-final-general-fallback-v1/general_provider.py"),
        model_unchanged=True, actual_root_restored=True, snapshot_released=True,
        provider_closed=True, anchor_snapshot_released=True, native_pending_verified=False,
        native_fallback_verified=False, gates_closed=False, training_admissible=False,
        exclusions=EXCLUSIONS), "global search report identity or scope differs")
    duration(report["seconds"])
    if report["prior_sha"] != prior.sha256:
        raise ValueError("global search prior differs")
    records = report["records"]
    if [(row["stage"], row["altered"]) for row in records] != [
            (stage, altered) for stage in ("initial", "successor") for altered in (False, True)]:
        raise ValueError("global search paired roots missing")
    successor = next(group["tick"] for group in trace["boundaries"] if group["tick"] > 110)
    if ([history.root_tick for history in histories] != [110, successor]
            or not histories[1].extends(histories[0]) or any(history.rejections for history in histories)):
        raise ValueError("global search FAIR history progression differs")
    expected_replay = [dict(tick=group["tick"], fair_sha=group["fair_sha"])
        for group in trace["boundaries"] if group["tick"] < successor]
    if report["replayed"] != expected_replay:
        raise ValueError("global search replay differs")
    opponent = SimpleNamespace(checkpoint_sha=SOURCE_SHA, controller_sha=semantic_digest(
        dict(source=SOURCE_SHA, adapter="fair-final-source-a", temperature=1.)))
    factory = IncrementalRefillBeliefFactory(None, prior=prior, catalog=None,
        opponent=opponent, sidecar_factory=None)
    provider = GeneralHistorySearchProvider(factory, history=histories[0], anchor_seed=ANCHOR_SEED)
    try:
        anchor = records[0]["audit"]["world_manifest"]["construction"]["anchor"]
        conditional = split_seed(ANCHOR_SEED, "conditional-history", histories[0].sha256)
        accepted = [split_seed(conditional, f"attempt:{ordinal}") for ordinal in range(4)
            if semantic_digest([split_seed(conditional, f"attempt:{ordinal}")]) == anchor["seed_commitment"]]
        if len(accepted) != 1:
            raise ValueError("global search independent seed chain differs")
        completion = HistoryCycleCursor(prior, opening=histories[0].opening).sample(histories[0],
            seed=split_seed(accepted[0], "opponent-draw"))
        for index, row in enumerate(records):
            history = histories[index // 2]
            group = next(group for group in trace["boundaries"] if group["tick"] == history.root_tick)
            if history.root_fair_sha != group["fair_sha"]["0"]:
                raise ValueError("global search FAIR root differs from native trace")
            decisions = [dict(tick=group["tick"], fair_sha=group["fair_sha"]["0"],
                action=group["actions"]["0"]) for group in trace["boundaries"]
                if group["tick"] < history.root_tick and "0" in group["fair_sha"]]
            deployments = sorted([dict(tick=event["tick"], owner=event["owner"], card_id=event["card_id"],
                x=int(event["position"][0]), y=int(event["position"][1]))
                for tick in trace["ticks"] if history.opening.tick < tick["tick"] <= history.root_tick
                for event in tick["public_executions"]], key=lambda event: (event["tick"], event["owner"]))
            if (payload([asdict(value) for value in history.decisions]) != decisions
                    or payload([asdict(value) for value in history.deployments]) != deployments):
                raise ValueError("global search recorded FAIR history differs from native trace")
            if index == 2:
                completion = advance_completion(completion,
                    history.deployments[len(histories[0].deployments):], owner=1)
            provider.history = history
            validate_world(row, history, provider, opponent)
            validate_layouts(row, history, completion)
        for pair in (records[:2], records[2:]):
            for name in ("outcome", "fair_sha", "history_sha", "tick", "attempts", "world_lifecycle",
                    "anchor_completion", "anchor_layouts", "local_layouts"):
                if pair[0][name] != pair[1][name]:
                    raise ValueError("global search actual hidden intervention influenced search")
            if pair[0]["actual_digest_before"] != pair[1]["mutation"]["before_digest"]:
                raise ValueError("global search hidden intervention baseline differs")
    finally:
        provider.close()
        factory.close()
    counts = validate_audits(report["snapshot_audits"])
    validate_spans(records, report["snapshot_audits"], report["snapshot_audit_spans"])
    return counts


def validate(attempt, launch_sha, receipt_sha):
    if sha(attempt / "launch.json") != launch_sha or sha(attempt / "download.json") != receipt_sha:
        raise ValueError("global search external archive identities differ")
    launch = json.loads((attempt / "launch.json").read_text())
    prepared = preparation(attempt, launch["preparation_sha256"])
    expected = dict(prepared, schema="FairFinalGlobalSearchLaunch/v1", process=launch["process"],
        preparation_sha256=launch["preparation_sha256"], preflight=launch["preflight"], status="launched")
    if launch != expected or launch["preflight"]["memory_available"] < 32 * 1024**3 or launch["preflight"]["free_bytes"] < 5 * 1024**3:
        raise ValueError("global search launch or resource identity differs")
    receipt = json.loads((attempt / "download.json").read_text())
    if (receipt["schema"] != "FairFinalGlobalSearchDownload/v1"
            or receipt["launch_sha256"] != launch_sha or receipt["worker_stopped"] is not True
            or any(receipt[name] is not False for name in ("training_admissible", "gates_closed"))):
        raise ValueError("global search receipt scope differs")
    archive = attempt / "archive"
    def read(name):
        return json.loads((archive / name).read_text())
    required = {*launch["payload_sha256"], "completion.json", "worker.log",
        "tick-110-seat-0/report.json", "tick-110-seat-0/cleanup.json", "tick-110-seat-0/probe-process.json",
        "tick-110-seat-0/progress.json"}
    required.update("tick-110-seat-0/history-" + stage + "-" + str(altered) + ".json"
        for stage in ("initial", "successor") for altered in (0, 1))
    required.update("tick-110-seat-0/engine-" + str(port) + "/process.json" for port in PORTS)
    if not required.issubset(receipt["files"]) or any(Path(name).name == "failure.json" for name in receipt["files"]):
        raise ValueError("global search archive incomplete or failed")
    for name, expected_sha in receipt["files"].items():
        path = archive / name
        if (Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink()
                or sha(path) != expected_sha):
            raise ValueError("global search archived bytes differ")
    if any(receipt["files"].get(name) != expected_sha for name, expected_sha in launch["payload_sha256"].items()):
        raise ValueError("global search deployed payload differs from preparation")
    equal_fields(read("completion.json"), dict(status="completed_requires_independent_verification",
        training_admissible=False, gates_closed=False), "global search completion scope differs")
    if read("tick-110-seat-0/probe-process.json")["exit_code"] != 0:
        raise ValueError("global search probe process failed")
    cleanup = read("tick-110-seat-0/cleanup.json")
    if cleanup["clean"] is not True or sorted(row["port"] for row in cleanup["engines"]) != PORTS:
        raise ValueError("global search engine cleanup missing")
    children = set()
    for row in cleanup["engines"]:
        process = read("tick-110-seat-0/engine-" + str(row["port"]) + "/process.json")
        if (row["clean"] is not True or row["port_released"] is not True or row["process"] != process
                or process["phase"] != "stopped" or process["port"] != row["port"]
                or row["launcher"] != dict(pid=process["pid"], start_ticks=process["start_ticks"])
                or process["phase_snapshot_engine"]["sha256"] != launch["settings"]["engine_sha256"]
                or process["phase_snapshot_engine"]["path"] != launch["remote"] + "/libcrprobe.so"
                or process["phase_snapshot_engine"]["launcher_sha256"] != launch["payload_sha256"]["project/phase_snapshot_launcher.py"]):
            raise ValueError("global search owned process identity or cleanup differs")
        children.add((process["child_pid"], process["child_start_ticks"]))
    if len(children) != 2:
        raise ValueError("global search requires independent native engines")
    report = read("tick-110-seat-0/report.json")
    histories = [FairHistory.from_payload(read("tick-110-seat-0/history-" + stage + "-0.json"))
        for stage in ("initial", "successor")]
    for index, stage in enumerate(("initial", "successor")):
        if read("tick-110-seat-0/history-" + stage + "-1.json") != payload(histories[index].to_payload()):
            raise ValueError("global search actual hidden intervention changed FAIR history")
    if (report["input_trace_sha256"] != sha(archive / "project/input-trace.json")
            or read("tick-110-seat-0/progress.json") != dict(records=report["records"], replayed=report["replayed"])):
        raise ValueError("global search source trace or incremental progress differs")
    counts = validate_report(report, histories, read("project/input-trace.json"),
        FrozenDeckPrior.from_payload(read("project/public-deck-prior-engineering.json")),
        launch["settings"]["source_sha256"], launch["settings"]["engine_sha256"])
    return dict(schema="FairFinalGlobalSearchVerification/v1", status="verified_limited_scope",
        launch_sha256=launch_sha, receipt_sha256=receipt_sha, searches=4, valid_simulations=512,
        hidden_pairs=2, snapshot_operations=counts, archived_files=len(receipt["files"]),
        engine_sha256=launch["settings"]["engine_sha256"],
        complete_game_verified=False, native_pending_verified=False, native_fallback_verified=False,
        hidden_random_coverage_closed=False, training_admissible=False, gates_closed=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--launch-sha", required=True)
    parser.add_argument("--receipt-sha", required=True)
    arguments = parser.parse_args()
    result = validate(arguments.attempt, arguments.launch_sha, arguments.receipt_sha)
    result["verifier_sha256"] = sha(__file__)
    with (arguments.attempt / "verification.json").open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
