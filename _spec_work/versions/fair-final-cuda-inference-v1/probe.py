import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import torch


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inspect_outputs(model, cases):
    from native_runner.training.fair_final.adapter import source_policy
    from native_runner.training.fair_final.policy import FullAction, LegalPrefixes
    from native_runner.training.fair_final.rollout import DecisionInformation
    from native_runner.training.fair_search.contracts import unpack

    results = []
    with torch.inference_mode():
        for case in cases:
            encoded = case["information"]
            legality = LegalPrefixes(encoded["card_ids"], unpack(encoded["plays"]), wait=True)
            frame = unpack(encoded["frame"])
            info = DecisionInformation(case["owner"], case["tick"], frame, legality)
            if info.sha256 != encoded["fair_sha"]:
                raise ValueError("frozen FAIR frame identity differs")
            policy, logits = source_policy(model, frame, legality)
            probabilities = logits.softmax(-1)
            if logits.shape != (1, 3) or not bool(torch.isfinite(probabilities).all()):
                raise ValueError("invalid WDL output")
            prefixes = [()]
            actions = [FullAction()]
            if bool(legality.cards.any()):
                prefixes.append((1,))
                for card in torch.where(legality.cards)[0].tolist():
                    prefixes.append((1, card))
                    cells = torch.where(legality.cells[card])[0].tolist()
                    for cell in sorted({cells[0], cells[len(cells) // 2], cells[-1]}):
                        prefixes.append((1, card, cell))
                        delays = torch.where(legality.plays[card, cell])[0].tolist()
                        for delay in sorted({delays[0], delays[-1]}):
                            actions.append(FullAction(legality.card_ids[card], cell, delay))
            conditionals = []
            for prefix in prefixes:
                distribution = policy.conditionals(prefix).exp().cpu()
                mask = legality.mask(prefix).cpu()
                if (not bool(torch.isfinite(distribution).all())
                        or bool((distribution[~mask] != 0).any())
                        or abs(float(distribution.sum()) - 1.) > 1e-5):
                    raise ValueError("conditional probability mass differs")
                conditionals.append(dict(prefix=list(prefix), probabilities=distribution.tolist()))
            greedy, _ = policy.sample(torch.Generator().manual_seed(20261022), greedy=True)
            sampled, _ = policy.sample(torch.Generator().manual_seed(20261022))
            if not legality.legal(greedy) or not legality.legal(sampled):
                raise ValueError("inference selected an illegal action")
            joint = [float(policy.log_prob(action)) for action in actions]
            if not all(torch.isfinite(torch.tensor(joint))):
                raise ValueError("nonfinite legal joint probability")
            results.append(dict(tick=info.tick, owner=info.owner, fair_sha=info.sha256,
                wdl=probabilities.cpu().tolist(), conditionals=conditionals,
                actions=[asdict(action) for action in actions], joint_logp=joint,
                greedy=asdict(greedy), sampled=asdict(sampled)))
    return results


def compare(reference, actual):
    errors = dict(conditional_probability=0., joint_logp=0., wdl=0.)
    if len(reference) != len(actual):
        raise ValueError("inference case coverage differs")
    for expected, observed in zip(reference, actual):
        for name in ("tick", "owner", "fair_sha", "actions", "greedy", "sampled"):
            if expected[name] != observed[name]:
                raise ValueError("inference identity or selected action differs: " + name)
        if len(expected["conditionals"]) != len(observed["conditionals"]):
            raise ValueError("conditional prefix coverage differs")
        for first, second in zip(expected["conditionals"], observed["conditionals"]):
            if first["prefix"] != second["prefix"]:
                raise ValueError("conditional prefix differs")
            difference = (torch.tensor(first["probabilities"]) - torch.tensor(second["probabilities"])).abs().max()
            errors["conditional_probability"] = max(errors["conditional_probability"], float(difference))
        for name in ("joint_logp", "wdl"):
            difference = (torch.tensor(expected[name]) - torch.tensor(observed[name])).abs().max()
            errors[name] = max(errors[name], float(difference))
    if errors["conditional_probability"] > 1e-5 or errors["wdl"] > 1e-5 or errors["joint_logp"] > 1e-4:
        raise ValueError("CPU/GPU output tolerance exceeded: " + repr(errors))
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    identity = json.loads((args.root / "identity.json").read_text())
    for relative, expected in identity["files"].items():
        if sha(args.root / relative) != expected:
            raise ValueError("deployed input differs: " + relative)
    if sha(args.checkpoint) != identity["checkpoint_sha256"]:
        raise ValueError("frozen source checkpoint differs")
    sys.path.insert(0, str(args.root / "project"))
    from native_runner.training.fair_search.contracts import semantic_digest
    from native_runner.training.fair_search.model import load_checkpoint

    torch.set_num_threads(1)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    cases = torch.load(args.root / "frames.pt", weights_only=True, map_location="cpu")
    model, _, model_sha = load_checkpoint(args.checkpoint,
        device="cpu", expected_sha256=identity["checkpoint_sha256"])
    model.eval().requires_grad_(False)
    before = semantic_digest(model.state_dict())
    cpu = inspect_outputs(model, cases)
    model.to("cuda:0")
    gpu = inspect_outputs(model, cases)
    torch.cuda.synchronize()
    errors = compare(cpu, gpu)
    if semantic_digest(model.state_dict()) != before:
        raise ValueError("inference mutated model parameters or buffers")
    report = dict(schema="FairFinalProjectCudaInference/v1", status="passed_limited_scope",
        training_admissible=False, gates_closed=False, checkpoint_sha256=model_sha,
        network_sha=before, identity_sha256=sha(args.root / "identity.json"),
        torch_version=torch.__version__, compiled_cuda=torch.version.cuda,
        device=torch.cuda.get_device_name(0), cases=len(cases),
        conditional_prefixes=sum(len(case["conditionals"]) for case in cpu),
        joint_actions=sum(len(case["actions"]) for case in cpu),
        errors=errors, model_unchanged=True, peak_allocated_bytes=torch.cuda.max_memory_allocated(0),
        cpu=cpu, gpu=gpu, seconds=time.monotonic()-started)
    with (args.root / "result.json").open("x") as handle:
        json.dump(report, handle, sort_keys=True, allow_nan=False)
    print(json.dumps({key: value for key, value in report.items() if key not in ("cpu", "gpu")}), flush=True)


if __name__ == "__main__":
    main()
