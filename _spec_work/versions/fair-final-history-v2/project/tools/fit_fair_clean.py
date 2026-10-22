import argparse
import json
from pathlib import Path

import torch

from fit_fair_consumer import fit_variant
from native_runner.training.event_checkpoint import load_event_checkpoint
from native_runner.training.fair_search.clean_experiment import (
    configure_deterministic_training, file_sha, identical_state, load_cohort, publish,
)
from native_runner.training.fair_search.model import load_checkpoint


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--device", default="cuda:7")
    args = parser.parse_args(argv)
    torch.set_num_threads(1)
    configure_deterministic_training()
    root = args.root
    output = root / "student"
    output.mkdir(exist_ok=True)
    manifest = json.loads((root / "manifest.json").read_text())
    episodes, experiment = load_cohort(root, manifest)
    if manifest.get("continuous_training", False):
        _, payload, _ = load_checkpoint(root / "initial.pt", expected_sha256=manifest["checkpoint_sha256"])
        credit = payload["sample_credit"]
        if type(credit) is not int or not 0 <= credit < manifest["config"]["batch_size"]:
            raise ValueError("continuous training sample remainder differs")
        budget = experiment["sample_budget"] + credit
        steps, remainder = divmod(budget, manifest["config"]["batch_size"])
        experiment.update(resume_initial_optimizer=True, incoming_sample_credit=credit,
                          sample_budget=budget, steps=steps, sample_remainder=remainder,
                          patience=steps + 2)
    experiment["code_sha256"] = {str(path.relative_to(root / "project")): file_sha(path)
        for path in (Path(__file__), Path(__file__).with_name("fit_fair_consumer.py"),
        root / "project/native_runner/training/fair_search/learner.py",
        root / "project/native_runner/training/fair_search/consumer.py",
        root / "project/native_runner/training/fair_search/clean_experiment.py")}
    marker = output / "experiment.json"
    if marker.exists() and json.loads(marker.read_text()) != experiment:
        raise ValueError("offline experiment identity changed")
    publish(marker, experiment)
    result = fit_variant(root / "initial.pt", episodes, experiment, output, mode="balanced", device=args.device)
    model, _, _ = load_checkpoint(root / "initial.pt", expected_sha256=manifest["checkpoint_sha256"], device=args.device)
    best = torch.load(output / "best.pt", map_location="cpu", weights_only=True)
    initial_state = {key: value.detach().cpu().clone() for key, value in model.policy.state_dict().items()}
    model.load_state_dict(best["model"], strict=True)
    exported = load_event_checkpoint(output / "policy.pt", device=args.device).model
    if not identical_state(model.policy, exported):
        raise ValueError("student policy export changed parameters or buffers")
    model.eval()
    exported.eval()
    validation = [episodes[key] for key in sorted(episodes) if manifest["split"][key] == "validation"]
    from native_runner.training.fair_search.joint_decode import score_actions
    with torch.inference_mode():
        for episode in validation:
            row = episode.decisions[0]
            context, _ = model.encode(row.information.frame)
            frame = row.information.frame.to_model_input(args.device, float_dtype=torch.float32)
            other = exported.encode(frame, exported.initial_state(frame.batch_size, device=args.device),
                    episode_start=torch.ones(frame.batch_size, dtype=torch.bool, device=args.device))
            masks = {name: value.to(args.device) for name, value in row.information.masks.items()}
            first = score_actions(model.policy, context, masks, row.visits)
            second = score_actions(exported, other, masks, row.visits)
            if any(abs(first[key] - second[key]) > 1e-6 for key in first):
                raise ValueError("student export changed fixed-frame action probabilities")
    unchanged = all(torch.equal(initial_state[key], value.cpu()) for key, value in model.policy.state_dict().items())
    result.update(policy_unchanged=unchanged, skip_evaluation=best["step"] == 0 and unchanged,
                  export_probability_check=True, sample_budget=experiment["sample_budget"],
                  training_rows=experiment["training_rows"])
    publish(output / "ready.json", result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
