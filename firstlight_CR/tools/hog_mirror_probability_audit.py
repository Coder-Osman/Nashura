#!/usr/bin/env python3
"""Run one native Hog Cycle mirror and audit responses to confirmed Hog plays.

The observer records the policy distribution only. It never changes masks,
sampling uniforms, action selection, or native scheduling.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch

from native_runner.card_names import CARD_NAMES
from tools import event_rl_rollout as rollout
from tools.league_decks import HOG_CYCLE_DECK

HOG_RIDER_ID = 26000021


def _scene_summary(observation: Any, owner: int) -> dict[str, Any]:
    """Keep only decision-relevant public state for a compact audit."""
    players = []
    for item in observation.players:
        if int(item.owner) != owner:
            continue
        players.append({
            "owner": int(item.owner),
            "elixir": round(float(item.elixir_exact), 4),
            "hand": [card_label(int(card)) for card in item.hand],
            "cycle": [card_label(int(card)) for card in item.cycle],
        })
    entities = []
    for item in observation.entities:
        if item.card_id is None:
            continue
        entities.append({
            "entity_id": int(item.entity_id),
            "owner": None if item.owner is None else int(item.owner),
            "card": card_label(int(item.card_id)),
            "card_id": int(item.card_id),
            "kind": str(item.entity_kind),
            "x": round(float(item.position[0]), 1),
            "y": round(float(item.position[1]), 1),
            "hp": None if item.hitpoints is None else round(float(item.hitpoints), 1),
            "age_ms": None if item.age_ms is None else int(item.age_ms),
            "target": None if item.visible_target is None else int(item.visible_target),
            "phase": item.attack_phase,
            "status": list(item.status),
        })
    towers = [
        {
            "owner": int(item.owner),
            "kind": str(item.tower_kind),
            "hp": round(float(item.hitpoints), 1),
            "max_hp": round(float(item.max_hitpoints), 1),
        }
        for item in observation.towers
    ]
    return {"tick": int(observation.tick), "players": players, "towers": towers, "entities": entities}


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def card_label(card_id: int) -> str:
    return CARD_NAMES.get(int(card_id), (str(card_id), str(card_id)))[0]


class HogResponseObserver:
    """Pairs each confirmed Hog event with the other side's first usable decision."""

    def __init__(self, destination: Path, *, delay_offset_ms: tuple[int, ...]) -> None:
        self.destination = destination
        self.delay_offset_ms = delay_offset_ms
        self.pending: dict[int, dict[str, Any]] = {}
        self.responses: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def __call__(self, kind: str, **payload: Any) -> None:
        if kind == "native_events":
            for item in payload["events"]:
                if str(item.get("kind")) != "card_play" or int(item.get("cardId", -1)) != HOG_RIDER_ID:
                    continue
                hog = {
                    "native_event_id": str(item["nativeEventId"]),
                    "deployment_id": str(item.get("deploymentId", "")),
                    "hog_owner": int(item["owner"]),
                    "hog_tick": int(item["tick"]),
                }
                self.events.append(hog)
                self.pending[1 - hog["hog_owner"]] = hog
            return
        if kind != "decision":
            return
        owner = int(payload["owner"])
        hog = self.pending.pop(owner, None)
        legal = payload["legal"]
        if hog is None or not bool(legal.decision_ready):
            return
        observation = payload["observation"]
        player = next(item for item in observation.players if int(item.owner) == owner)
        probabilities = payload["distribution"]
        if probabilities is None:
            raise RuntimeError("missing direct policy distribution")
        rows = []
        for row, allowed in enumerate(legal.card_mask.detach().cpu().tolist()):
            if not allowed:
                continue
            card_id = int(legal.row_card_id[row])
            rows.append(
                {
                    "row": row,
                    "card_id": card_id,
                    "card": card_label(card_id),
                    "native_hand_slot": int(legal.hand_index[row]),
                    "p_card_given_act": float(probabilities["card"][row]),
                }
            )
        rows.sort(key=lambda item: item["p_card_given_act"], reverse=True)
        step = payload["step"]
        chosen_row = int(step.action.card_row[0]) if bool(step.action.act[0]) else None
        chosen_card = None if chosen_row is None else int(legal.row_card_id[chosen_row])
        card_argmax = rows[0] if rows else None
        delay_probs = probabilities.get("delay") if isinstance(probabilities, dict) else None
        delay_argmax = None
        if delay_probs is not None:
            delay_argmax = {
                "delay_bin": max(range(len(delay_probs)), key=lambda index: delay_probs[index]),
                "delay_ms": self.delay_offset_ms[max(range(len(delay_probs)), key=lambda index: delay_probs[index])],
                "probability": max(delay_probs),
            }
        response = {
            **hog,
            "response_owner": owner,
            "response_tick": int(payload["tick"]),
            "trigger": str(payload["trigger"]),
            "elixir": float(player.elixir_exact),
            "hand": [
                {"card_id": int(card_id), "card": card_label(int(card_id))}
                for card_id in player.hand
            ],
            "p_wait": float(probabilities["gate"][0]),
            "p_act": float(probabilities["gate"][1]),
            "legal_cards": rows,
            "sampled": {
                "gate": "ACT" if bool(step.action.act[0]) else "WAIT",
                "card_row": chosen_row,
                "card_id": chosen_card,
                "card": None if chosen_card is None else card_label(chosen_card),
                "delay_bin": None if chosen_row is None else int(step.action.delay_bin[0]),
                "delay_ms": None if chosen_row is None else int(
                    self.delay_offset_ms[int(step.action.delay_bin[0])]
                ),
            },
            "argmax": {
                "card": None if card_argmax is None else card_argmax["card"],
                "card_probability": None if card_argmax is None else card_argmax["p_card_given_act"],
                "delay": delay_argmax,
            },
            "scene": _scene_summary(observation, owner),
        }
        self.responses.append(response)
        with self.destination.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n")


class AuditEnvironment(rollout.BattleEnvV1):
    def __init__(self, *args, artifact_root: Path, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.artifact_root = artifact_root
        self.final_terminal = None
        self.native_result = None

    def reset(self, *args, **kwargs):
        result = super().reset(*args, **kwargs)
        (self.artifact_root / "render-match.json").write_text(self.match_config.to_json())
        atomic_json(self.artifact_root / "initial.json", dict(self.raw_observation))
        return result

    def _terminal(self, raw, towers):
        result = super()._terminal(raw, towers)
        if result.ended:
            self.final_terminal = dataclasses.asdict(result)
            self.native_result = {key: raw.get(key) for key in ("winner", "ended", "tick", "crowns")}
        return result


def write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# 速猪镜像跳猪响应概率审计",
        "",
        f"- 权重：`{summary['checkpoint_sha256']}`（RL {summary['iteration']}）",
        f"- 固定种子：`{summary['seed']}`",
        f"- 终局：`{summary['terminal']}`",
        "",
        "卡牌概率是当前合法掩码下的 `p(卡牌 | ACT)`；`p(ACT)` 与该条件概率分开列出。",
        "",
    ]
    responses = summary["hog_responses"]
    if not responses:
        lines.extend(["本局没有确认执行的野猪，或野猪后对方没有产生可用决策。", ""])
    for index, item in enumerate(responses, 1):
        lines.extend([
            f"## 第 {index} 次确认跳猪",
            "",
            f"野猪方：owner {item['hog_owner']}，确认 tick {item['hog_tick']}；响应方：owner {item['response_owner']}，决策 tick {item['response_tick']}。",
            "",
            f"圣水 `{item['elixir']:.2f}`；`p(ACT)={item['p_act']:.4f}`，`p(WAIT)={item['p_wait']:.4f}`；实际采样：`{item['sampled']['gate']}`"
            + ("" if item["sampled"]["card"] is None else (
                f"，`{item['sampled']['card']}`，计划延迟 `{item['sampled']['delay_ms']} ms`"
            ))
            + "。",
            "",
            "| 合法卡牌 | 概率 `p(卡牌 | ACT)` | 手牌槽位 |",
            "| --- | ---: | ---: |",
        ])
        for card in item["legal_cards"]:
            lines.append(f"| {card['card']} | {card['p_card_given_act']:.4%} | {card['native_hand_slot']} |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=2026092901)
    parser.add_argument("--port", type=int, default=26789)
    parser.add_argument("--max-ticks", type=int, default=7200)
    parser.add_argument("--max-seconds", type=float, default=1500.0)
    args = parser.parse_args()

    root = args.out_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for prior in (root / "hog-responses.jsonl", root / "native-actions.json"):
        prior.unlink(missing_ok=True)
    shutil.copy2(args.checkpoint, root / "latest-rl1755.pt")
    loaded = rollout.load_event_checkpoint(args.checkpoint, device="cpu")
    if loaded.checkpoint_sha256 != args.checkpoint.name.removesuffix(".pt"):
        raise ValueError("checkpoint file name and verified SHA differ")
    model = loaded.model
    model.eval()
    torch.set_num_threads(1)
    bundle = rollout.production_semantic_bundle()
    proxies, coordinator = rollout.build_resident_training_proxies_v4(
        (0,), host="127.0.0.1", port=args.port, timeout=90.0, step_until_event=True
    )
    proxy = proxies[0]
    original_queue = proxy.queue_hand_action_at
    native_actions: list[dict[str, Any]] = []

    def queue(action, **kwargs):
        raw_player = next(
            item for item in environment.raw_observation["players"] if int(item["owner"]) == int(action.owner)
        )
        raw_card = next(
            item for item in raw_player["hand"] if int(item["handIndex"]) == int(action.hand_index)
        )
        card_id = int(raw_card["cardId"])
        receipt = original_queue(action, **kwargs)
        native_actions.append(
            {
                "owner": int(action.owner),
                "card_id": card_id,
                "card": card_label(card_id),
                "native_hand_slot": int(action.hand_index),
                "world_x": int(action.x),
                "world_y": int(action.y),
                "receipt": dict(receipt),
            }
        )
        atomic_json(root / "native-actions.json", native_actions)
        return receipt

    proxy.queue_hand_action_at = queue
    environment = AuditEnvironment(
        native=proxy,
        card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=rollout.RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
        ruleset_id=hashlib.sha256(f"hog-mirror-probabilities:{args.seed}".encode()).hexdigest(),
        terminal_margin_beta=0.0,
        artifact_root=root,
    )
    observer = HogResponseObserver(
        root / "hog-responses.jsonl", delay_offset_ms=tuple(model.event_config.delay_offset_ms)
    )
    sampling_generator = torch.Generator(device="cpu").manual_seed(args.seed)
    started = time.time()
    recorder, counters = rollout.play_match(
        environment=environment,
        coordinator=coordinator,
        model=model,
        opponent_model=None,
        event_config=model.event_config,
        catalog=model.backbone.catalog,
        device=torch.device("cpu"),
        decks={0: HOG_CYCLE_DECK, 1: HOG_CYCLE_DECK},
        match_id=f"hog-mirror-{args.seed}",
        policy_version="RL-1755",
        seed=args.seed,
        env_id=0,
        temperatures=(1.0, 1.0, 1.0, 1.0),
        max_ticks=args.max_ticks,
        max_seconds=args.max_seconds,
        max_advance_ticks=30,
        max_wait_ticks=300,
        first_match=True,
        behavior_checkpoint_sha256=loaded.checkpoint_sha256,
        behavior_model_contract_hash=loaded.contract_sha256,
        sampling_generator=sampling_generator,
        decision_observer=observer,
    )
    summary = {
        "checkpoint_sha256": loaded.checkpoint_sha256,
        "iteration": 1755,
        "seed": args.seed,
        "deck": {"name": "hog-cycle", "cards": list(HOG_CYCLE_DECK)},
        "temperatures": [1.0, 1.0, 1.0, 1.0],
        "terminal": environment.final_terminal,
        "native_result": environment.native_result,
        "counters": counters,
        "native_actions": len(native_actions),
        "confirmed_hogs": observer.events,
        "hog_responses": observer.responses,
        "wall_seconds": time.time() - started,
    }
    atomic_json(root / "summary.json", summary)
    write_report(root / "report.md", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
