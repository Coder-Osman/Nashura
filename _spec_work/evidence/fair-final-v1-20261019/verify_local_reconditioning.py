from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2]/"firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.local_reconditioning import SettledDrawReconditioner
from native_runner.training.fair_final.prior_history import HistoryCycleCursor, PriorHistoryBeliefFactory
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


EXCLUSIONS = ["Full128-search", "actual-hidden-state-intervention", "single-ready-pending-plan",
    "incomplete-refills", "end-to-end-throughput", "training-admission"]
MODEL_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"


def validate_phase_engine_build(manifest):
    if (manifest["schema"] != "fair-final-phase-snapshot-build.v1"
            or manifest["training_admissible"] is not False
            or manifest["scope"] != "single-slot-phase-snapshot-and-local-draw"
            or manifest["exclusions"] != ["multi-slot-telemetry", "Full128-search", "training-admission"]
            or manifest["builder_sha256"] != hashlib.sha256(
                (ROOT/"build_phase_snapshot_engine.py").read_bytes()).hexdigest()):
        raise ValueError("phase snapshot native build identity differs")
    output = Path(manifest["commands"][-1][-1]).resolve()
    if output.name != "libcrprobe.so" or not output.is_relative_to(ROOT.resolve()):
        raise ValueError("phase snapshot build is outside isolated evidence")
    for relative, expected in manifest["inputs"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("phase snapshot input path escapes workspace")
        if hashlib.sha256((SOURCE.parent/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("phase snapshot native source changed: "+relative)
    for relative, expected in manifest["artifacts"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("phase snapshot artifact path escapes build")
        if hashlib.sha256((output.parent/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("phase snapshot native artifact changed: "+relative)
    return manifest["artifacts"]["libcrprobe.so"]


def validate_local_reconditioning(report=None, *, root=ROOT):
    if report is None:
        report = json.loads((root/"native-local-reconditioning-report.json").read_text())
    if (report["schema"] != "fair-final-local-reconditioning-suite.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or report["ports"] != [29660, 29661]
            or not report["source_sha256"] or not report["artifacts"]
            or "native_runner/training_capture.py" not in report["source_sha256"]):
        raise ValueError("local suite identity or decoder source missing")
    for name, key in (("run_local_reconditioning_validation.py", "runner_sha256"),
            ("local_reconditioning_worker.py", "worker_sha256")):
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != report[key]:
            raise ValueError("local runner identity changed")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((SOURCE/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("local source changed: "+relative)
    for relative, expected in report["artifacts"].items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("local artifact path escapes evidence root")
        if hashlib.sha256((root/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("local artifact changed: "+relative)

    def artifact_path(name):
        paths = [root/relative for relative in report["artifacts"] if relative.endswith("/"+name)]
        if len(paths) != 1:
            raise ValueError("local artifact missing or ambiguous: "+name)
        return paths[0]

    def artifact(name):
        return json.loads(artifact_path(name).read_text())

    attempt, registration, preflight = artifact("attempt.json"), artifact("run-manifest.json"), artifact("preflight.json")
    expected_engine = ENGINE_SHA
    if "engine_path" in registration:
        if registration["engine_path"] != report["remote"]+"/libcrprobe.so":
            raise ValueError("local engine path escapes isolated attempt")
        expected_engine = validate_phase_engine_build(artifact("engine-build-manifest.json"))
    if (artifact("project/probe-source-manifest.json") != report["source_sha256"]
            or hashlib.sha256(artifact_path("project/run.py").read_bytes()).hexdigest() != report["worker_sha256"]
            or attempt["exit_code"] != 0 or any(attempt[key] != report[key] for key in
                ("remote", "ports", "source_sha256", "runner_sha256", "worker_sha256", "prior_file_sha"))
            or registration["root"] != report["remote"] or registration["ports"] != report["ports"]
            or preflight["memory_available"] < 32*1024**3 or preflight["free_bytes"] < 5*1024**3
            or registration["engine_sha256"] != preflight["engine_sha256"]
            or preflight["engine_sha256"] != expected_engine
            or preflight.get("baseline_engine_sha256", ENGINE_SHA) != ENGINE_SHA):
        raise ValueError("local deployment or resource evidence differs")
    native, cleanup = report["report"], report["cleanup"]
    if (artifact("report.json") != native or artifact("cleanup.json") != cleanup
            or native["schema"] != "fair-final-native-local-reconditioning.v1" or native["status"] != "passed"
            or native["training_admissible"] is not False or native["model_unchanged"] is not True
            or native["source_sha256"] != report["source_sha256"]
            or native["checkpoint_sha256"] != MODEL_SHA or native["exclusions"] != EXCLUSIONS):
        raise ValueError("local raw result scope or identity differs")
    if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
            or len(cleanup["engines"]) != 2 or {entry["port"] for entry in cleanup["engines"]} != {29660, 29661}
            or len({entry["process"]["child_pid"] for entry in cleanup["engines"]}) != 2
            or any(entry["listening"] or any(entry["pids_alive"].values()) or entry["process"]["phase"] != "stopped"
                for entry in cleanup["engines"])):
        raise ValueError("local engine cleanup incomplete")
    if "engine_path" in registration:
        launcher_sha = hashlib.sha256((ROOT/"phase_snapshot_launcher.py").read_bytes()).hexdigest()
        if hashlib.sha256(artifact_path("project/phase_snapshot_launcher.py").read_bytes()).hexdigest() != launcher_sha:
            raise ValueError("local engine launcher source changed")
        for entry in cleanup["engines"]:
            identity = entry["process"]["phase_snapshot_engine"]
            if (identity["sha256"] != expected_engine or identity["path"] != registration["engine_path"]
                    or identity["launcher_sha256"] != launcher_sha
                    or entry["process"]["argv"].count(registration["engine_path"]) != 1):
                raise ValueError("running engine identity differs from isolated build")
            require_sha(identity["service_sha256"])
    prior_file = root/"public-deck-prior-engineering.json"
    prior = FrozenDeckPrior.from_payload(artifact(prior_file.name))
    if (prior.to_payload() != json.loads(prior_file.read_text()) or prior.sha256 != native["prior_sha"]
            or hashlib.sha256(prior_file.read_bytes()).hexdigest() != report["prior_file_sha"]):
        raise ValueError("local prior identity differs")
    expected = [(threshold, owner, seed) for threshold in (1200, 3600) for owner in (0, 1)
        for seed in (78102201+owner, 78102202+owner, 78102201+owner)]
    records = native["records"]
    if (len(records) != 12 or [(row["threshold"], row["owner"], row["seed"]) for row in records] != expected
            or artifact("progress.json")["records"] != records):
        raise ValueError("local A/B/A root coverage or progress differs")
    cursors, signatures, cold_profiles = {}, {}, {}
    for row in records:
        owner, threshold, seed = row["owner"], row["threshold"], row["seed"]
        history_path = artifact_path(f"history-{threshold}-{owner}.json")
        history = FairHistory.from_payload(json.loads(history_path.read_text()))
        manifest = row["manifest"]
        if (row["history_file_sha"] != hashlib.sha256(history_path.read_bytes()).hexdigest()
                or row["history_sha"] != history.sha256 or row["tick"] != history.root_tick
                or row["tick"] < threshold or history.opening.owner != owner
                or row["anchor_restored"] is not True or row["native_phase_restored"] is not True
                or type(row["native_phase_object_count"]) is not int or row["native_phase_object_count"] <= 0
                or manifest["schema"] != SettledDrawReconditioner.schema
                or manifest["source"] != "independent-belief" or manifest["actual_state_copied"] is not False
                or manifest["root_fair_sha"] != history.root_fair_sha or manifest["history_sha"] != history.sha256
                or manifest["root_tick"] != history.root_tick or manifest["owner"] != owner
                or manifest["prior_sha"] != prior.sha256 or manifest["sampler_sha"] != native["sampler_sha"]
                or manifest["seed_commitment"] != semantic_digest([seed])
                or any(manifest[key] is not True for key in
                    ("native_draw_verified", "public_and_own_fair_unchanged", "controller_unchanged"))
                or manifest["history_ticks_replayed"] != 0 or manifest["training_admissible"] is not False
                or manifest["scope"] != "settled-root-local-draw-engineering"
                or manifest["opponent_checkpoint_sha"] != MODEL_SHA):
            raise ValueError("local FAIR root or independent world evidence differs")
        opponent = SimpleNamespace(controller_sha=manifest["opponent_controller_sha"], checkpoint_sha=MODEL_SHA)
        expected_controller = semantic_digest(dict(source=MODEL_SHA, adapter="fair-final-source-a"))
        factory = PriorHistoryBeliefFactory(None, prior=prior, catalog=None, sidecar_factory=None, opponent=opponent)
        if (opponent.controller_sha != expected_controller
                or SettledDrawReconditioner(factory).sampler_sha != native["sampler_sha"]):
            raise ValueError("local sampler or frozen opponent identity differs")
        if owner not in cursors:
            cursors[owner] = HistoryCycleCursor(prior, opening=history.opening)
        completion = cursors[owner].sample(history, seed=seed)
        if (json.loads(json.dumps(asdict(completion))) != row["completion"]
                or completion.manifest != manifest["completion_manifest"]
                or cursors[owner].last_delta != row["cycle_delta"]
                or manifest["native_current_draw"] != [*sorted(completion.current_draw[:4]), *completion.current_draw[4:]]):
            raise ValueError("local public-cycle completion or installed draw differs")
        continuations = row["continuations"]
        if (len(continuations) != 2 or continuations[0] != continuations[1]
                or continuations[0]["completed"] is not True or not 1 <= continuations[0]["ticks"] <= 120
                or not 1 <= continuations[0]["decisions"] <= 6):
            raise ValueError("local snapshot continuation differs")
        for digest in (row["anchor_digest"], row["local_digest"], continuations[0]["native_digest"],
                manifest["opponent_fair_sha"]):
            require_sha(digest)
        seconds = row["seconds"]
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("local timing invalid")
        root_key = threshold, owner
        profile = row["cold_profile"]
        if (profile["cold_ticks"] != history.root_tick-history.opening.tick
                or profile["incremental_ticks"] != 0 or profile["ticks"] != profile["cold_ticks"]
                or profile["syncs"] != 1 or not math.isfinite(profile["seconds"]) or profile["seconds"] <= 0
                or cold_profiles.setdefault(root_key, profile) != profile):
            raise ValueError("local cold anchor profile differs")
        signature = dict(manifest=manifest, digest=row["local_digest"], anchor=row["anchor_digest"],
            continuation=continuations[0])
        if signatures.setdefault((threshold, owner, seed), signature) != signature:
            raise ValueError("local repeated seed depends on preceding completion")
    return report


if __name__ == "__main__":
    result = validate_local_reconditioning()
    print(json.dumps(dict(status="passed", roots=4, replacements=len(result["report"]["records"]))))
