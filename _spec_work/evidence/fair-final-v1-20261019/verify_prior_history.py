import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
SOURCE = WORKSPACE/"firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.prior_history import HistoryCycleCursor, PriorHistoryBeliefFactory
from native_runner.training.fair_final.search import split_seed
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


def validate_prior_history(report=None, *, root=ROOT):
    report = report if report is not None else json.loads((root/"native-prior-history-report.json").read_text())
    if (report["schema"] != "fair-final-prior-history-suite.v1" or report["status"] != "passed"
            or report["training_admissible"] is not False or report["ports"] != [29650, 29651]
            or not report["source_sha256"] or not report["artifacts"]):
        raise ValueError("native prior suite identity differs")
    for name, key in (("run_prior_history_validation.py", "runner_sha256"),
            ("prior_history_worker.py", "worker_sha256")):
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != report[key]:
            raise ValueError("native prior runner identity changed")
    for relative, expected in report["source_sha256"].items():
        if hashlib.sha256((SOURCE/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("native prior source changed: "+relative)
    for relative, expected in report["artifacts"].items():
        if hashlib.sha256((root/relative).read_bytes()).hexdigest() != expected:
            raise ValueError("native prior artifact changed: "+relative)

    def artifact(name):
        paths = [root/relative for relative in report["artifacts"] if relative.endswith("/"+name)]
        if len(paths) != 1:
            raise ValueError("native prior artifact missing or ambiguous: "+name)
        return json.loads(paths[0].read_text())

    native, cleanup = report["report"], report["cleanup"]
    attempt, registration, preflight = artifact("attempt.json"), artifact("run-manifest.json"), artifact("preflight.json")
    if (artifact("project/probe-source-manifest.json") != report["source_sha256"]
            or [value for key,value in report["artifacts"].items() if key.endswith("/project/run.py")] != [report["worker_sha256"]]
            or attempt["exit_code"] != 0 or any(attempt[key] != report[key] for key in
                ("remote", "ports", "source_sha256", "runner_sha256", "worker_sha256", "prior_file_sha"))
            or registration["root"] != report["remote"] or registration["ports"] != report["ports"]
            or preflight["memory_available"] < 32*1024**3 or preflight["free_bytes"] < 5*1024**3
            or registration["engine_sha256"] != preflight["engine_sha256"]
            or preflight["engine_sha256"] != "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"):
        raise ValueError("native prior deployment or preflight differs")
    if (artifact("report.json") != native or artifact("cleanup.json") != cleanup
            or native["schema"] != "fair-final-native-prior-history.v1" or native["status"] != "passed"
            or native["training_admissible"] is not False or native["model_unchanged"] is not True
            or native["source_sha256"] != report["source_sha256"]
            or native["checkpoint_sha256"] != "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
            or native["thresholds"] != [1200, 3600]
            or native["exclusions"] != ["Full128-search", "native-local-reconditioning", "population-prior-calibration",
                "normal-single-ready-root", "training-admission", "end-to-end-throughput"]):
        raise ValueError("native prior result scope or raw report differs")
    if (cleanup["exit_code"] != 0 or cleanup["error"] is not None or cleanup["clean"] is not True
            or len(cleanup["engines"]) != 2 or {entry["port"] for entry in cleanup["engines"]} != {29650,29651}
            or len({entry["process"]["child_pid"] for entry in cleanup["engines"]}) != 2
            or any(entry["listening"] or any(entry["pids_alive"].values()) or entry["process"]["phase"] != "stopped"
                for entry in cleanup["engines"])):
        raise ValueError("native prior engine cleanup incomplete")
    prior_path = root/"public-deck-prior-engineering.json"
    prior = FrozenDeckPrior.from_payload(artifact(prior_path.name))
    if (prior.to_payload() != json.loads(prior_path.read_text()) or prior.sha256 != native["prior_sha"]
            or native["prior_file_sha"] != report["prior_file_sha"]
            or hashlib.sha256(prior_path.read_bytes()).hexdigest() != report["prior_file_sha"]):
        raise ValueError("native prior identity differs")
    expected = {(threshold,owner,variant) for threshold in (1200,3600) for owner in (0,1)
        for variant in ("original","hidden-draw-changed")}
    records = native["records"]
    if len(records) != 8 or {(row["threshold"],row["owner"],row["variant"]) for row in records} != expected:
        raise ValueError("native prior paired coverage missing")
    for row in records:
        owner, threshold = row["owner"], row["threshold"]
        history = FairHistory.from_payload(artifact(f"history-{threshold}-{owner}.json"))
        conditioning = row["conditioning"]
        if (row["tick"] != history.root_tick or row["tick"] < threshold or history.opening.owner != owner
                or row["history_sha"] != history.sha256 or row["fair_sha"] != history.root_fair_sha
                or any(row[key] is not True for key in ("root_restored","actual_match_unchanged","snapshot_released"))
                or conditioning["native_draw_verified"] is not True or conditioning["training_admissible"] is not False
                or conditioning["actual_state_copied"] is not False or conditioning["source"] != "independent-belief"
                or conditioning["history_sha"] != history.sha256 or conditioning["root_fair_sha"] != history.root_fair_sha
                or conditioning["root_tick"] != history.root_tick or conditioning["owner"] != owner
                or conditioning["prior_sha"] != prior.sha256 or conditioning["sampler_sha"] != native["sampler_sha"]):
            raise ValueError("native prior FAIR root or world identity differs")
        factory = PriorHistoryBeliefFactory(None, prior=prior, catalog=None, sidecar_factory=None,
            opponent=SimpleNamespace(controller_sha=conditioning["opponent_controller_sha"],
                checkpoint_sha=conditioning["opponent_checkpoint_sha"]))
        if factory.sampler_sha != native["sampler_sha"]:
            raise ValueError("native prior sampler identity differs")
        seeds = [split_seed(78102031+owner, f"attempt:{ordinal}") for ordinal in range(4)]
        seeds = [seed for seed in seeds if semantic_digest([seed]) == conditioning["seed_commitment"]]
        if len(seeds) != 1:
            raise ValueError("native prior accepted attempt seed missing")
        cursor = HistoryCycleCursor(prior, opening=history.opening)
        completion = cursor.sample(history, seed=split_seed(seeds[0],"opponent-draw"))
        if (completion.manifest != conditioning["completion_manifest"]
                or list(completion.initial_draw) != row["initial_draw"]
                or list(completion.current_draw) != row["current_draw"]):
            raise ValueError("native prior sampled completion differs")
        current = conditioning["native_current_draw"]
        expected_draw = completion.current_draw
        if (type(current) is not list or len(current) != 8 or len(set(current)) != 8
                or any(type(card) is not int for card in current)
                or set(current[:4]) != set(expected_draw[:4]) or tuple(current[4:]) != expected_draw[4:]
                or conditioning["logical_hand_order_differs"] is not (tuple(current[:4]) != expected_draw[:4])):
            raise ValueError("native prior hand membership, FIFO or slot evidence differs")
        continuations = row["continuations"]
        if (len(continuations) != 2 or continuations[0] != continuations[1]
                or continuations[0]["completed"] is not True or not 1 <= continuations[0]["ticks"] <= 120
                or not 1 <= continuations[0]["decisions"] <= 6):
            raise ValueError("native prior snapshot continuation differs")
        require_sha(row["root_native_digest"])
        require_sha(continuations[0]["native_digest"])
        mutation = row["mutation"]
        if row["variant"] == "original":
            if mutation is not None:
                raise ValueError("original prior variant contains mutation")
        else:
            if mutation["before_digest"] == mutation["after_digest"]:
                raise ValueError("native hidden draw did not change")
            require_sha(mutation["before_digest"])
            require_sha(mutation["after_digest"])
            original = next(item for item in records if item["threshold"] == threshold
                and item["owner"] == owner and item["variant"] == "original")
            if any(original[key] != row[key] for key in ("conditioning","root_native_digest",
                    "continuations","initial_draw","current_draw","fair_sha","history_sha")):
                raise ValueError("native hidden draw changed independent prior world")
    incremental = native["incremental"]
    if len(incremental) != 4 or {(row["threshold"],row["owner"]) for row in incremental} != {
            (threshold,owner) for threshold in (1200,3600) for owner in (0,1)}:
        raise ValueError("native prior successor coverage missing")
    for row in incremental:
        owner, threshold = row["owner"], row["threshold"]
        history = FairHistory.from_payload(artifact(f"history-{threshold}-{owner}.json"))
        following = FairHistory.from_payload(artifact(f"history-{threshold}-{owner}-next.json"))
        cursor = HistoryCycleCursor(prior, opening=history.opening)
        cursor.sync(history)
        delta = cursor.sync(following)
        sample = cursor.sample(following, seed=78102131+owner)
        if (not following.extends(history) or following.root_tick <= history.root_tick
                or row["tick"] != following.root_tick or row["history_sha"] != following.sha256
                or row["exact"] is not True or row["delta"] != delta
                or row["completion_manifest"] != sample.manifest):
            raise ValueError("native prior successor incremental evidence differs")
    return report


if __name__ == "__main__":
    report = validate_prior_history()
    print(json.dumps(dict(status="passed", roots=len(report["report"]["records"]),
        successors=len(report["report"]["incremental"]))))
