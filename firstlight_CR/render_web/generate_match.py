"""Generate an exact native trace on the dedicated remote engine."""

import dataclasses
import hashlib
import json
from pathlib import Path
import sys
import time


def resolve_decks(request, legacy_registry=None):
    # Old requests retain the frozen registry's original slot order on retry.
    if "deck_cards" not in request:
        return {owner: tuple(legacy_registry[request["deck_" + side]])
                for owner, side in enumerate(("top", "bottom"))}
    cards = request["deck_cards"]
    if not isinstance(cards, dict) or set(cards) != {"top", "bottom"}:
        raise ValueError("invalid deck snapshot")
    for deck in cards.values():
        if (not isinstance(deck, list) or len(deck) != 8
                or any(type(card) is not int or card <= 0 for card in deck)
                or len(set(deck)) != 8):
            raise ValueError("deck snapshot requires eight unique card IDs")
    return {owner: tuple(cards[side]) for owner, side in enumerate(("top", "bottom"))}


def validate_rollout(counters, terminal, rows, action_count, same_model,
                     *, minimum_delay_ms=None, delay_offset_ms=()):
    if not counters["terminal"] or terminal["reason"] != "native_final_result":
        raise ValueError("对局尚未到真实终局")
    if counters["scheduler_version"] != "event-scheduling.v2":
        raise ValueError("调度版本不匹配")
    if any(counters[k] for k in ("not_ready", "cancelled_window", "act_violation")):
        raise ValueError("对局有未执行或违规动作")
    if counters["executed"] != action_count:
        raise ValueError("双方原生出牌计数不一致")
    if minimum_delay_ms is not None:
        for lane in rows.values():
            if any(row.act and (not 0 <= row.delay_bin < len(delay_offset_ms)
                                or delay_offset_ms[row.delay_bin] < minimum_delay_ms) for row in lane):
                raise ValueError("对局包含低于最短延迟的动作")
    # The training recorder deliberately excludes history-opponent learning rows.
    # Native actions above still capture both policies, regardless of learner sides.
    for owner in (0, 1) if same_model else (0,):
        lane = rows[owner]
        if not lane or not lane[-1].done or any(x.truncated for x in lane):
            raise ValueError("学习轨迹存在截断或缺少真实终局")


def main():
    code, output, port = sys.argv[1:]
    sys.path.insert(0, code)
    import torch
    from tools import event_rl_rollout as r

    root = Path(output)
    request = json.loads((root / "request.json").read_text())
    decks = resolve_decks(request, r._deck_registry() if "deck_cards" not in request else None)

    def save(name, value):
        p = root / name
        t = p.with_suffix(p.suffix + ".tmp")
        t.write_text(json.dumps(value))
        t.replace(p)

    torch.set_num_threads(1)
    loaded = [
        r.load_event_checkpoint(root / (side + ".pt"), device="cpu")
        for side in ("top", "bottom")
    ]
    for side, x in zip(("top", "bottom"), loaded):
        assert (
            x.checkpoint_sha256 == request[side]["sha256"]
            and x.model.backbone.config.snapshot_only
        )
        x.model.eval()
    a, b = [x.payload["event_checkpoint_contract"] for x in loaded]
    for key in (
        "input_semantics",
        "action_semantics",
        "backbone_config",
        "event_config",
    ):
        assert a[key] == b[key], "checkpoint compatibility: " + key

    class Env(r.BattleEnvV1):
        def reset(self, *args, **kwargs):
            result = super().reset(*args, **kwargs)
            (root / "render-match.json").write_text(self.match_config.to_json())
            save("initial.json", dict(self.raw_observation))
            return result

        def step(self, *args, **kwargs):
            if (root / "CANCEL").exists():
                raise InterruptedError("cancelled")
            result = super().step(*args, **kwargs)
            save(
                "progress.json",
                dict(tick=self.raw_observation.get("tick", 0), actions=len(actions)),
            )
            return result

        def _terminal(self, raw, towers):
            result = super()._terminal(raw, towers)
            if result.ended:
                self.final_terminal = dataclasses.asdict(result)
                self.native_result = {
                    k: raw.get(k) for k in ("winner", "ended", "tick", "crowns")
                }
            return result

    bundle = r.production_semantic_bundle()
    proxies, coordinator = r.build_resident_training_proxies_v4(
        (0,), host="127.0.0.1", port=int(port), timeout=90.0, step_until_event=True
    )
    proxy = proxies[0]
    original = proxy.queue_hand_action_at
    actions = []

    def queue(action, **kwargs):
        receipt = original(action, **kwargs)
        actions.append(
            dict(
                owner=action.owner,
                world_x=action.x,
                world_y=action.y,
                receipt=dict(receipt),
            )
        )
        save("native-actions.json", actions)
        return receipt

    proxy.queue_hand_action_at = queue
    env = Env(
        native=proxy,
        card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=r.RuntimeEffectCatalog.from_catalog(
            bundle.native_effect_catalog
        ),
        ruleset_id=hashlib.sha256(b"render-web.v1").hexdigest(),
        terminal_margin_beta=0.0,
    )
    seed = request["seed"]
    torch.manual_seed(seed)
    start = time.time()
    same = loaded[0].checkpoint_sha256 == loaded[1].checkpoint_sha256
    with torch.inference_mode():
        recorder, counters = r.play_match(
            environment=env,
            coordinator=coordinator,
            model=loaded[0].model,
            opponent_model=None if same else loaded[1].model,
            event_config=loaded[0].model.event_config,
            catalog=loaded[0].model.backbone.catalog,
            device=torch.device("cpu"),
            decks=decks,
            match_id=root.name,
            policy_version="render-web",
            seed=seed,
            env_id=0,
            temperatures=(1.0, 1.0, 1.0, 1.0),
            max_ticks=7200,
            max_seconds=1500.0,
            max_advance_ticks=30,
            max_wait_ticks=300,
            first_match=True,
            behavior_checkpoint_sha256=loaded[0].checkpoint_sha256,
            behavior_model_contract_hash=loaded[0].contract_sha256,
        )
    constraints = request.get("action_constraints")
    validate_rollout(counters, env.final_terminal, recorder.rows, len(actions), same,
                     minimum_delay_ms=constraints["delay"]["minimum_ms"] if constraints else None,
                     delay_offset_ms=loaded[0].model.event_config.delay_offset_ms)
    save("native-actions.json", actions)
    stats = dict(
        seed=seed,
        owner_checkpoint_sha256=[x.checkpoint_sha256 for x in loaded],
        decks=[request["deck_top"], request["deck_bottom"]],
        deck_cards={side: list(decks[owner]) for owner, side in enumerate(("top", "bottom"))},
        iterations=[request[s]["iteration"] for s in ("top", "bottom")],
        level=11,
        temperatures=[1, 1, 1, 1],
        contracts=[x.contract_sha256 for x in loaded],
        terminal=env.final_terminal,
        native=env.native_result,
        counters=counters,
        wall_s=time.time() - start,
        action_constraints=constraints,
        runtime_fingerprint=Path(code).name,
    )
    save("stats.json", stats)
    with (root / "trajectory.jsonl").open("w") as stream:
        for action in actions:
            stream.write(json.dumps(action) + "\n")
    print(json.dumps(stats), flush=True)


if __name__ == "__main__":
    main()
