import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT.parent / "fair-final-migration-selection-v1"),
    str(ROOT.parent / "fair-final-migration-series-v1"), str(ROOT.parents[2] / "firstlight_CR")]

import torch

from round_selection import SCHEMA, _selection, SelectionConfig
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checkpoint_state(path, expected_sha):
    if sha(path) != require_sha(expected_sha):
        raise ValueError("checkpoint file identity differs")
    envelope = torch.load(path, weights_only=True, map_location="cpu")
    payload = envelope["payload"]
    if (envelope["schema"] != SCHEMA or payload["schema"] != SCHEMA
            or envelope["sha256"] != semantic_digest(payload)):
        raise ValueError("selection checkpoint semantic identity differs")
    latest, best = payload["latest"], payload["best"]
    for record in (latest, best):
        if record["sha256"] != semantic_digest(record["payload"]):
            raise ValueError("nested trainer checkpoint identity differs")
    trainer = latest["payload"]
    cursor = trainer["cursor"]
    epochs = cursor["frozen_epochs"] + cursor["trainable_epochs"]
    selected, _, terminal = _selection(payload["history"], SelectionConfig(**payload["config"]),
        cursor["frozen_epochs"], epochs)
    if selected != payload["best_index"] or terminal != payload["terminal"]:
        raise ValueError("checkpoint validation selection differs")
    if semantic_digest(best["payload"]["network"]) != payload["history"][selected]["network_sha"]:
        raise ValueError("checkpoint selected network differs")
    ids = [] if cursor["completed_epochs"] == epochs else [trainer["splits"]["train"][index]
        for index in cursor["order"][cursor["offset"]:cursor["offset"] + cursor["batch_size"]]]
    state = dict(network=semantic_digest(trainer["network"]),
        optimizer=semantic_digest(trainer["optimizer"]), rng=semantic_digest(trainer["rng"]),
        sampler=semantic_digest(dict(order=cursor["order"], rng=cursor["rng"],
            offset=cursor["offset"], consumed=cursor["consumed"], epochs=cursor["completed_epochs"])),
        next_ids=ids, steps=cursor["optimizer_steps"], teacher=trainer["teacher_network_sha"],
        selection=semantic_digest(dict(history=payload["history"], best=best,
            best_index=selected, terminal=terminal)))
    return state, trainer["identity"], trainer["splits"]


def verify_comparison(directory, *, reference_sha, resumed_sha):
    directory = Path(directory)
    for name, expected in (("reference", reference_sha), ("resumed", resumed_sha)):
        if sha(directory / (name + ".json")) != require_sha(expected):
            raise ValueError("comparison report checksum differs")
    reference = json.loads((directory / "reference.json").read_text())
    resumed = json.loads((directory / "resumed.json").read_text())
    if (reference["training_admissible"] is not False or reference["formal_g2_pass"] is not False
            or resumed["training_admissible"] is not False or resumed["formal_g2_pass"] is not False
            or resumed["status"] != "passed_limited_scope"
            or resumed["exact_model_optimizer_rng_sampler_selection"] is not True):
        raise ValueError("engineering comparison scope differs")
    middle, identity, splits = checkpoint_state(directory / "midpoint.pt", reference["midpoint_sha256"])
    expected, expected_identity, expected_splits = checkpoint_state(
        directory / "reference.pt", reference["reference_checkpoint_sha256"])
    actual, actual_identity, actual_splits = checkpoint_state(
        directory / "resumed.pt", resumed["resumed_checkpoint_sha256"])
    if identity != expected_identity or identity != actual_identity or splits != expected_splits or splits != actual_splits:
        raise ValueError("comparison changed dataset identity or whole-game splits")
    if (middle != reference["midpoint_state"] or expected != reference["expected"]
            or actual != resumed["actual"] or expected != actual
            or resumed["metrics"] != reference["next_metrics"]):
        raise ValueError("comparison report differs from checkpoint content")
    before = reference["before"]
    if (middle["steps"] != before["steps"] + 1 or actual["steps"] != middle["steps"] + 1
            or before["teacher"] != middle["teacher"] or middle["teacher"] != actual["teacher"]
            or before["network"] == middle["network"] or middle["network"] == actual["network"]
            or reference["next_rows"] != middle["next_ids"]):
        raise ValueError("comparison did not perform two matching real updates")
    used = reference["first_rows"] + reference["next_rows"]
    if not used or not set(used).issubset(splits["train"]) or set(used) & set(splits["validation"] + splits["test"]):
        raise ValueError("comparison optimization crossed whole-game splits")
    return dict(status="passed_checkpoint_content_scope", steps=actual["steps"],
        dataset_identity=identity, state=actual, training_admissible=False, formal_g2_pass=False,
        native_deployment_verified=False, reference_report_sha256=reference_sha, resumed_report_sha256=resumed_sha)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--reference-sha", required=True)
    parser.add_argument("--resumed-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify_comparison(args.directory, reference_sha=args.reference_sha, resumed_sha=args.resumed_sha)
    report["verifier_sha256"] = sha(__file__)
    durable_write(args.output, json_bytes(report))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
