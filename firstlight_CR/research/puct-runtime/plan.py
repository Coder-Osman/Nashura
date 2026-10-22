"""Strict plan builder for the fixed 2.6 Hog learner."""
from __future__ import annotations

import hashlib
import gzip
import json
from pathlib import Path
import sys
import torch

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

DECK_ID = "hog"
DECK = (26000021, 26000038, 26000014, 26000010, 26000030, 27000000, 28000000, 28000011)
FORM_MASK = (0,) * 8
RULES = {"card_form": "base", "level": 11, "tower": "princess", "minimum_delay_ms": 100}
FLEET = {"34": 24, "35": 24, "37": 24, "39": 18, "41": 18}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_training_state(path: Path, config: dict) -> dict:
    payload = torch.load(path, map_location="cpu", weights_only=True)
    saved = payload.get("event_ppo_training")
    if not isinstance(saved, dict) or saved.get("version") != "event-ppo-training.v1":
        raise ValueError("1755 checkpoint lacks resumable PPO state")
    if saved.get("iterations") != config["start_iteration"] or saved.get("optimizer_type") != "AdamW":
        raise ValueError("1755 iteration or optimizer type mismatch")
    optimizer = saved.get("optimizer")
    if (not isinstance(optimizer, dict) or not optimizer.get("state") or
            not optimizer.get("param_groups") or not saved.get("parameter_names")):
        raise ValueError("1755 AdamW moments or parameter identity missing")
    if "event_component_gae" in payload or payload.get("rl_gae_config") is not None:
        raise ValueError("1755 uses scalar GAE; the proposed continuation must retain it")
    reward = payload.get("rl_reward_config")
    if not isinstance(reward, dict) or reward.get("version") != "elixir-overflow-tower.v6":
        raise ValueError("1755 reward contract mismatch")
    reference = payload.get("rl_reference_policy")
    if reference != {"version": "reference-disabled.v1", "enabled": False} or payload.get("kl_coefficient") != 0.0:
        raise ValueError("1755 reference KL contract mismatch")
    if config["optimizer"]["gamma"] != .999 or config["optimizer"]["gae_lambda"] != .95:
        raise ValueError("continuation must retain scalar GAE defaults")
    return {"iterations": saved["iterations"], "optimizer_steps": saved["optimizer_steps"],
            "optimizer_type": saved["optimizer_type"], "optimizer_state_entries": len(optimizer["state"]),
            "reward_config": reward, "gae": {"gamma": .999, "lambda": .95, "type": "scalar"},
            "reference_kl": reference, "kl_coefficient": 0.0}


def build_plan(config: dict) -> dict:
    if config["learner_deck_id"] != DECK_ID:
        raise ValueError("hog tree-search plan requires learner deck hog")
    if (len(DECK) != 8 or len(set(DECK)) != 8 or RULES["tower"] != "princess" or
            RULES["card_form"] != "base" or RULES["level"] != 11):
        raise ValueError("frozen Hog deck or base-rule contract changed")
    if config["fleet"] != FLEET or sum(FLEET.values()) != 108:
        raise ValueError("five-host 108-engine fleet mismatch")
    if sum(config["round_games"].values()) != 108:
        raise ValueError("round allocation must total 108 games")
    tree = config["tree_search"]
    if (tree["state_version"] != "tree-search-state.v1" or
            tree.get("state_mode") != "deterministic_full_state"):
        raise ValueError("tree-search state mode contract mismatch")
    reward = config["reward"]
    if abs(reward["q_expected_weight"] + reward["q_robust_weight"] - 1.0) > 1e-9:
        raise ValueError("Q target weights must sum to one")
    if config["evaluation"]["games"] != 6000 or config["evaluation"]["tree_search"]:
        raise ValueError("final evaluation must be 6000 games with search disabled")
    if config["optimizer"]["ppo_updates"] != 200:
        raise ValueError("plan must contain 200 PPO updates")
    if config["optimizer"]["updates_per_cycle"] != 20 or config["optimizer"]["ppo_epochs_per_update"] != 1:
        raise ValueError("PPO update cadence differs from the approved plan")
    if config["round_games"] != {"self_play": 72, "fixed_1250": 12, "recent": 24}:
        raise ValueError("Hog round opponent allocation mismatch")
    catalog = PROJECT / "native_runner/data/competitive/card_specs.json.gz"
    support = PROJECT / "native_runner/data/competitive/card_support.json"
    with gzip.open(catalog, "rt") as stream:
        specs = json.load(stream)["specs"]
    costs = {int(row["card_id"]): row["elixir_cost"] for row in specs if row.get("card_id") in DECK}
    if set(costs) != set(DECK) or any(type(value) not in (int, float) for value in costs.values()):
        raise ValueError("Hog card cost catalog incomplete")
    support_rows = {int(row["card_id"]): int(row["form_availability"])
                    for row in json.loads(support.read_text())["supported_cards"]
                    if row["card_id"] in DECK}
    if set(support_rows) != set(DECK):
        raise ValueError("Hog form availability catalog incomplete")
    identity = {"deck_id": DECK_ID, "card_ids": list(DECK), "tower_troop": RULES["tower"],
                "form_mask": list(FORM_MASK), "form_availability": [support_rows[card] for card in DECK],
                "average_elixir": sum(costs[card] for card in DECK) / 8,
                "card_specs_sha256": sha256(catalog), "card_support_sha256": sha256(support)}
    identity["sha256"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema": "hog-tree-search-match-plan.v1",
        "start_iteration": config["start_iteration"],
        "start_checkpoint_sha256": config["start_checkpoint_sha256"],
        "learner_deck_id": DECK_ID,
        "learner_deck": list(DECK),
        "learner_deck_identity": identity,
        "cycles": 10,
        "teacher_games_per_cycle": 216,
        "ppo_updates_per_cycle": 20,
        "round_games": config["round_games"],
        "fleet": config["fleet"],
        "tree_search": config["tree_search"],
        "distillation": config["distillation"],
        "evaluation": config["evaluation"],
    }


def main() -> None:
    root = Path(__file__).resolve().parent
    config = json.loads((root / "config.json").read_text())
    plan = build_plan(config)
    plan["start_checkpoint_present"] = (root / config["start_checkpoint"]).resolve().exists()
    plan["start_checkpoint_sha256_verified"] = plan["start_checkpoint_present"] and sha256(
        (root / config["start_checkpoint"]).resolve()) == config["start_checkpoint_sha256"]
    if not plan["start_checkpoint_sha256_verified"]:
        raise SystemExit("1755 checkpoint is missing or has the wrong SHA-256")
    plan["source_training_state"] = source_training_state((root / config["start_checkpoint"]).resolve(), config)
    (root / "match-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
