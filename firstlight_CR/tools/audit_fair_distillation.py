"""P0: decode frozen policies and prepare FAIR-only roots for clean teacher audits."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import random

import torch

from native_runner.training.fair_search.contracts import InformationState, semantic_digest
from native_runner.training.fair_search.joint_decode import action_key, joint_beam, score_actions
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.storage import atomic_write, load_episode, torch_bytes


def publish(path, record):
    atomic_write(path, json.dumps(record, sort_keys=True, allow_nan=False).encode(), replace=True)


def decode_record(model, row, width):
    info = row.information
    context, _ = model.encode(info.frame)
    device = next(model.parameters()).device
    masks = {key: value.to(device) for key, value in info.masks.items()}
    greedy = action_key(model.policy.select_action(context, **masks, sample=False, validate=True).action)
    beam = joint_beam(model.policy, context, masks, width=width)
    top = min(row.visits, key=lambda key: (-row.visits[key], key))
    actions = sorted(set(beam["log_probabilities"]) | set(row.visits) | {greedy})
    logs = score_actions(model.policy, context, masks, actions)
    if any(not info.legal(key) for key in actions):
        raise ValueError("audit decoder generated illegal action")
    return dict(greedy=greedy, joint_beam=beam["action"], noisy_teacher_mode=top,
                greedy_logp=logs[greedy], beam_logp=logs[beam["action"]], teacher_logp=logs[top],
                teacher_rank_in_audit_candidates=1 + sum(value > logs[top] for value in logs.values()),
                rank_scope="beam-and-replay-candidates-not-full-action-space", invalid_action_count=0)


def metric(records, decoder):
    act = [row for row in records if row["noisy_teacher_mode"] != "wait"]
    wait = [row for row in records if row["noisy_teacher_mode"] == "wait"]
    both = [row for row in act if row[decoder] != "wait"]
    card = [row for row in both if row[decoder].split(":")[1] == row["noisy_teacher_mode"].split(":")[1]]
    position = [row for row in card if row[decoder].split(":")[2] == row["noisy_teacher_mode"].split(":")[2]]
    definitions = {
        "teacher_wait_rate": (len(wait), len(records)),
        "student_ACT_given_teacher_ACT": (sum(row[decoder] != "wait" for row in act), len(act)),
        "student_WAIT_given_teacher_WAIT": (sum(row[decoder] == "wait" for row in wait), len(wait)),
        "exact_full_action_match_on_teacher_ACT": (sum(row[decoder] == row["noisy_teacher_mode"] for row in act), len(act)),
        "card_match_given_both_ACT": (len(card), len(both)),
        "position_match_given_correct_card": (len(position), len(card)),
        "latency_match_given_correct_card_and_position": (sum(row[decoder] == row["noisy_teacher_mode"] for row in position), len(position)),
        "full_action_match": (sum(row[decoder] == row["noisy_teacher_mode"] for row in records), len(records)),
        "greedy_vs_joint_disagreement_rate": (sum(row["greedy"] != row["joint_beam"] for row in records), len(records))}
    return {key: dict(numerator=numerator, denominator=denominator,
                     rate=numerator / denominator if denominator else None)
            for key, (numerator, denominator) in definitions.items()}


def report_group(records):
    episodes = sorted({row["episode_id"] for row in records})
    by_episode = {key: [row for row in records if row["episode_id"] == key] for key in episodes}
    output = {}
    for decoder in ("greedy", "joint_beam"):
        pooled = metric(records, decoder)
        pieces = {key: metric(values, decoder) for key, values in by_episode.items()}
        rng = random.Random(20261013)
        replicates = {key: [] for key in pooled}
        for iteration in range(1000):
            sample = rng.choices(episodes, k=len(episodes)) if episodes else []
            for key in pooled:
                denominator = sum(pieces[identity][key]["denominator"] for identity in sample)
                if denominator:
                    replicates[key].append(sum(pieces[identity][key]["numerator"] for identity in sample) / denominator)
        for key, values in replicates.items():
            values.sort()
            pooled[key]["episode_bootstrap95"] = [values[int(.025 * len(values))], values[min(len(values)-1, int(.975 * len(values)))]] if values else None
        output[decoder] = pooled
    output["episodes"] = len(episodes)
    output["rows"] = len(records)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cuda:7")
    args = parser.parse_args()
    torch.set_num_threads(1)
    args.output.mkdir(parents=True, exist_ok=True)
    plan = json.loads((args.comparison / "experiment.json").read_text())
    student_record = json.loads((args.comparison / "balanced/done.json").read_text())
    student_path = args.comparison / "balanced/best.pt"
    if hashlib.sha256(student_path.read_bytes()).hexdigest() != student_record["best.pt_sha256"]:
        raise ValueError("student source differs")
    initial, _, sha = load_checkpoint(args.source / "initial.pt", device=args.device, expected_sha256=plan["initial_sha256"])
    student, _, _ = load_checkpoint(args.source / "initial.pt", device=args.device, expected_sha256=sha)
    student.load_state_dict(torch.load(student_path, weights_only=True, map_location="cpu")["model"], strict=True)
    models = {"initial": initial.eval(), "student": student.eval()}
    identities = list(plan["split"])
    contract = dict(schema="fair-distillation-p0.v1", teacher_sha256=sha,
                    student_sha256=student_record["best.pt_sha256"], source_sha256=plan["source_sha256"],
                    split=plan["split"], beam_width=32, seed=20261013,
                    teacher_reference="original-exploratory-visits-pending-clean-research",
                    cancellation="execution-status-unknown-do-not-infer-from-selected-action",
                    unavailable_strata=["attack-defense", "actual-elixir-multiplier", "timeout-wait"],
                    code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    contract_path = args.output / "contract.json"
    if contract_path.exists() and json.loads(contract_path.read_text()) != contract:
        raise ValueError("P0 audit provenance differs")
    publish(contract_path, contract)
    roots, validation_records = [], {name: [] for name in models}

    def load(identity):
        directory = args.source / "replay" / identity
        marker = json.loads((directory / "completed.json").read_text())
        if marker["sha256"] != plan["source_sha256"][identity]:
            raise ValueError("P0 source episode differs")
        episode = load_episode(directory / "completed.json")
        if episode.current_sha256 != sha:
            raise ValueError("P0 teacher identity differs")
        return episode

    with ThreadPoolExecutor(max_workers=4) as executor, torch.inference_mode():
        for ordinal, episode in enumerate(executor.map(load, identities)):
            identity = episode.episode_id
            rng = random.Random(f"p0:{identity}:20261013")
            strata = {}
            for row in episode.decisions:
                top = min(row.visits, key=lambda key: (-row.visits[key], key))
                phase = "early" if row.information.tick < 1200 else "middle" if row.information.tick < 3600 else "late"
                strata.setdefault((row.information.owner, phase, top == "wait"), []).append(row)
            groups = list(strata.values())
            rng.shuffle(groups)
            selected = [rng.choice(group) for group in groups[:4]]
            selected_ids = {row.index for row in selected}
            available = [row for row in episode.decisions if row.index not in selected_ids]
            selected.extend(rng.sample(available, min(4 - len(selected), len(available))))
            root_indices = {row.index for row in selected}
            records = {name: [] for name in models}
            for row in episode.decisions:
                if plan["split"][identity] != "validation" and row.index not in root_indices:
                    continue
                info = row.information
                restored = InformationState.from_payload(info.to_payload())
                if restored.sha256 != info.sha256:
                    raise ValueError("FAIR observation roundtrip differs")
                metadata = dict(episode_id=identity, row_index=row.index, owner=info.owner, tick=info.tick,
                                observation_sha256=info.sha256, mask_sha256=semantic_digest(info.masks),
                                selected_plan=row.selected, execution_status="unknown", result=row.result)
                decoded = {name: dict(metadata, **decode_record(model, row, 32)) for name, model in models.items()}
                for name in models:
                    if plan["split"][identity] == "validation":
                        records[name].append(decoded[name])
                        validation_records[name].append(decoded[name])
                if row.index in root_indices:
                    key = f"{identity}-row{row.index:05d}"
                    root = dict(schema="fair-clean-audit-input.v1", information=info.to_payload(),
                                **metadata, original_visits=dict(row.visits), decoded=decoded,
                                teacher_sha256=sha, search_config_sha256=row.config_sha256,
                                runtime_sha256=row.runtime_sha256, split=plan["split"][identity])
                    destination = args.output / "roots" / (key + ".pt")
                    data = torch_bytes(root)
                    atomic_write(destination, data, replace=True)
                    roots.append(dict(key=key, path=str(destination), sha256=hashlib.sha256(data).hexdigest(),
                                      split=plan["split"][identity], owner=info.owner, tick=info.tick))
            publish(args.output / "episodes" / (identity + ".json"), records)
            publish(args.output / "roots.json", dict(schema="fair-clean-audit-queue.v1", roots=roots,
                    complete=False, teacher_sha256=sha, contract_sha256=semantic_digest(contract)))
            publish(args.output / "status.json", dict(stage="decoding", episodes=ordinal+1, required=len(identities), roots=len(roots)))
            print(json.dumps(dict(stage="decoded", episodes=ordinal+1, roots=len(roots))), flush=True)
    report = {name: report_group(records) for name, records in validation_records.items()}
    publish(args.output / "report.json", report)
    publish(args.output / "roots.json", dict(schema="fair-clean-audit-queue.v1", roots=roots,
            complete=True, teacher_sha256=sha, contract_sha256=semantic_digest(contract)))
    publish(args.output / "status.json", dict(stage="completed", roots=len(roots), promoted=False))


if __name__ == "__main__":
    main()
