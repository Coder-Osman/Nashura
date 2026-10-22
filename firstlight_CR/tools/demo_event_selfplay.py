"""Generate one headless event-policy self-play match as a renderable trajectory.

Both owners are driven by the same event-policy checkpoint: at every public
deployment event each owner observes the match from its own perspective, the
model picks (gate, card, delay, cell) in one free-running forward pass
(``card_row=None``, so the card head conditions on its own argmax rather than a
teacher row), and the choice is installed as a cancellable plan that executes
on an exact future tick.  This is the same plan/cancel contract the capture
labels describe, so the match is a faithful event-driven rollout rather than a
teacher-forced replay.

Only the engine work happens here; the match is rendered elsewhere by
``tools/replay_driver.py`` from the emitted trajectory + match payload.

Outputs in ``--out-dir``:
  trajectory.jsonl    one line per submitted play: {"tick", "actions": {"0": [...], "1": [...]}}
  render-match.json   the exact ``configure-native`` payload (decks + seed)
  stats.json          decode legality, delay histogram, plays, terminal result

Usage:
    .venv/bin/python tools/demo_event_selfplay.py \
        --checkpoint RUNS/event-pretrain-run/event-pretrain.pt \
        --deck0 golem-beatdown --deck1 pekka-bridge-spam \
        --out-dir RUNS/demo-a --port 26789 --env-id 0 --device cpu
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Mapping
import importlib.util
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402

from native_runner.battle_env import BattleEnvV1  # noqa: E402
from native_runner.contracts import ActionV1  # noqa: E402
from native_runner.match_factory import (  # noqa: E402
    TOURNAMENT_LEVEL,
    MatchConfig,
)
from native_runner.rich_telemetry_adapter import RuntimeEffectCatalog  # noqa: E402
from native_runner.training.event_action_space import (  # noqa: E402
    event_cell_to_native,
    event_placement_rows_to_model,
    native_hand_slots,
    legal_event_actions,
)
from native_runner.training.event_abilities import ability_action_from_plan, ability_plan
from native_runner.training.event_config import EventPolicyConfigV1  # noqa: E402
from native_runner.training.event_scheduling import (  # noqa: E402
    EVENT_SCHEDULER_VERSION, EventExecutionLedger, complete_event_commands,
    gameplay_end_tick, policy_actions_open,
)
from native_runner.training.event_spell_constraints import (  # noqa: E402
    LOG_CARD_ID,
    action_constraints_contract,
    add_lightning_target_arguments,
    add_log_target_arguments,
    constrain_spell_placement,
    lightning_constraint_from_args,
    log_constraint_from_args,
    spell_target_allowed,
)
from native_runner.training.v4.catalog import FIRST_REAL_CARD_VOCAB_ID  # noqa: E402
from native_runner.training.v4.expert import POLICY_DECISION_TICKS  # noqa: E402
from native_runner.training.v4.factory import (  # noqa: E402
    build_episode_tensorizer_v4,
    build_resident_training_proxies_v4,
    production_semantic_bundle,
)

RESET_OPTIONS = {
    "render_mode": "headless",
    "event_driven_decisions": False,
    "allow_initial_remaining_runtime_rejections": True,
}
TICK_MS = 50


def _load_deck_registry():
    path = Path(__file__).resolve().parent / "league_decks.py"
    spec = importlib.util.spec_from_file_location("_league_decks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # IL league decks plus the RL-specialist decks (adds lava-hound; the shared
    # names carry identical cards, so this only widens the lookup table).
    return {**module.LEAGUE_DECKS, **getattr(module, "RL_LEAGUE_DECKS", {})}


def legal_play_options(environment, observation, owner: int, *, lightning_constraint=None, log_constraint=None):
    """card_id -> (native hand_index, legal model cells), plus empty diagnostics."""

    reason = {"empty": "none", "elixir": None, "min_cost": None, "hand": 0}
    if not policy_actions_open(environment, int(observation.tick)):
        reason["empty"] = "gameplay-ended"
        return {}, reason
    player = next((item for item in observation.players if item.owner == owner), None)
    if player is not None:
        reason["elixir"] = float(player.elixir_exact)
        reason["hand"] = len(player.hand)
        try:
            raw_hand = next(
                (item["hand"] for item in environment.raw_observation["players"] if int(item["owner"]) == owner), []
            )
            costs = [int(card["cost"]) for card in raw_hand]
            reason["min_cost"] = min(costs) if costs else None
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
    if player is None or not player.hand:
        reason["empty"] = "no-hand"
        return {}, reason
    mask = environment.action_mask(owner)
    if not mask.kinds.get("play_card", False):
        reason["empty"] = "play-not-allowed"
        return {}, reason
    options: dict[int, tuple[int, list[tuple[int, int]]]] = {}
    for hand_index, card_id in native_hand_slots(player):
        if hand_index >= len(mask.hand_slots) or not mask.hand_slots[hand_index]:
            continue
        entry = mask.placement_masks.get(str(hand_index))
        if not isinstance(entry, Mapping):
            continue
        rows = entry.get("row_major")
        if not rows:
            continue
        rows = constrain_spell_placement(
            rows, observation, owner, card_id, lightning_constraint=lightning_constraint, log_constraint=log_constraint
        )
        rows = event_placement_rows_to_model(rows, owner=owner)
        cells = [(x, y) for y, row in enumerate(rows) for x, value in enumerate(row) if value]
        if cells:
            options[int(card_id)] = (int(hand_index), cells)
    if not options:
        reason["empty"] = "no-legal-cell-or-slot"
    return options, reason


class Decoder:
    """Free-running masked decode of one event observation."""

    def __init__(self, model, catalog, event_config, device: str):
        self.model = model
        self.catalog = catalog
        self.event_config = event_config
        self.device = device
        self.width = event_config.board_width
        self.first_vocab = FIRST_REAL_CARD_VOCAB_ID
        self.stats = {
            "decisions": 0,
            "gate_act": 0,
            "gate_wait": 0,
            "act_no_legal_play": 0,
            "card_raw_argmax_illegal": 0,
            "cell_raw_argmax_illegal": 0,
            "delay_hist": {},
            "plays": 0,
        }
        # Per-owner behaviour, so a side that stalls is visible instead of being
        # averaged away: decisions, gate choices, elixir at the decision, spend.
        self.per_owner = {
            owner: {
                "decisions": 0, "gate_act": 0, "gate_wait": 0, "act_no_legal_play": 0,
                "elixir_sum": 0.0, "elixir_cap": 0, "card_cost_sum": 0.0,
                "no_legal_reasons": {}, "no_legal_samples": [],
            }
            for owner in (0, 1)
        }

    def _row_to_card(self, vocab: int) -> int | None:
        index = vocab - self.first_vocab
        if 0 <= index < len(self.catalog.raw_card_ids):
            return int(self.catalog.raw_card_ids[index])
        return None

    def __call__(self, frame, observation, owner: int, options, reason=None, trigger="event", *, legal=None):
        batch = frame.to_model_input(self.device, float_dtype=torch.float32)
        state = self.model.initial_state(batch.batch_size, device=self.device)
        episode_start = torch.ones(batch.batch_size, dtype=torch.bool, device=self.device)
        with torch.inference_mode():
            # Encode once: the backbone is the expensive part and does not
            # depend on the choices, so every resolve step below re-runs only
            # the event heads.
            context = self.model.encode(batch, state, episode_start=episode_start)
            has_abilities = getattr(getattr(self.model, "config", None), "ability_actions", False)
            if has_abilities:
                if legal is None:
                    raise ValueError("ability decoder requires native event legality")
                step = self.model.select_action(context, sample=False,
                    gate_mask=torch.tensor([[True, legal.act_allowed]], device=self.device),
                    card_mask=legal.card_mask[None], cell_masks=legal.cell_masks[None],
                    delay_mask=legal.delay_masks[None])
                gate = int(step.action.gate_choice[0])
            else:
                output = self.model.heads(context)
                gate = int(output.gate_logits.argmax(dim=-1)[0])

        self.stats["decisions"] += 1
        own = self.per_owner[owner]
        own["decisions"] += 1
        own[f"decisions_{trigger}"] = own.get(f"decisions_{trigger}", 0) + 1
        elixir = next(
            (float(p.elixir_exact) for p in observation.players if int(p.owner) == int(owner)), 0.0
        )
        own["elixir_sum"] += elixir
        own["elixir_cap"] += int(elixir >= 9.5)
        at_cap = elixir >= 9.5
        if gate == 0:
            self.stats["gate_wait"] += 1
            own["gate_wait"] += 1
            own[f"wait_{trigger}"] = own.get(f"wait_{trigger}", 0) + 1
            if at_cap:
                own["wait_at_cap"] = own.get("wait_at_cap", 0) + 1
            return None
        self.stats["gate_act"] += 1
        own["gate_act"] += 1
        own[f"act_{trigger}"] = own.get(f"act_{trigger}", 0) + 1
        if at_cap:
            own["act_at_cap"] = own.get("act_at_cap", 0) + 1
        if has_abilities:
            if gate == 2:
                self.stats["abilities"] = self.stats.get("abilities", 0) + 1
                own["abilities"] = own.get("abilities", 0) + 1
                return ability_plan(frame, int(step.action.ability_candidate[0]),
                    owner=owner, tick=int(observation.tick),
                    ability_catalog=self.model.backbone.ability_catalog,
                    delay_ms=(self.event_config.bin_to_delay_ms(int(step.action.delay_bin[0]))
                              if self.model.config.ability_delay else 0))
            row = int(step.action.card_row[0])
            delay_ms = self.event_config.bin_to_delay_ms(int(step.action.delay_bin[0]))
            self.stats["delay_hist"][str(delay_ms)] = self.stats["delay_hist"].get(str(delay_ms), 0) + 1
            return dict(owner=owner, card_id=int(legal.row_card_id[row]),
                        hand_index=int(legal.hand_index[row]),
                        cell=event_cell_to_native(int(step.action.cell[0]), owner=owner, board_width=self.width),
                        delay_ms=int(delay_ms), scheduled_tick=None)
        if not options:
            self.stats["act_no_legal_play"] += 1
            own["act_no_legal_play"] += 1
            if reason is not None:
                tag = f"{reason['empty']}"
                own.setdefault("no_legal_reasons", {})
                own["no_legal_reasons"][tag] = own["no_legal_reasons"].get(tag, 0) + 1
                own.setdefault("no_legal_samples", [])
                if len(own["no_legal_samples"]) < 6:
                    own["no_legal_samples"].append(
                        {"tick": int(observation.tick), "elixir": reason["elixir"],
                         "min_cost": reason["min_cost"], "hand": reason["hand"]}
                    )
            return None

        vocab_rows = frame.own_cards.card_vocab_id[0]
        row_mask = frame.own_cards.mask[0]
        legal_rows: list[int] = []
        row_card: dict[int, int] = {}
        for row in range(int(vocab_rows.shape[0])):
            if not bool(row_mask[row]):
                continue
            card_id = self._row_to_card(int(vocab_rows[row]))
            if card_id is not None and card_id in options:
                legal_rows.append(row)
                row_card[row] = card_id
        if not legal_rows:
            self.stats["act_no_legal_play"] += 1
            return None

        card_logits = output.card_logits[0]
        if int(card_logits.argmax(dim=-1)) != int(card_logits[torch.tensor(legal_rows)].argmax(dim=-1)) and int(
            card_logits.argmax(dim=-1)
        ) not in legal_rows:
            self.stats["card_raw_argmax_illegal"] += 1
        chosen_row = legal_rows[int(card_logits[torch.tensor(legal_rows, device=card_logits.device)].argmax(dim=-1))]
        card_id = row_card[chosen_row]
        hand_index, cells = options[card_id]
        # Re-run the heads under the card actually played. Legality picks among
        # the legal rows, so conditioning on the raw argmax card would place a
        # card the actor cannot afford and time it for the wrong placement.
        card_tensor = torch.tensor([chosen_row], device=card_logits.device, dtype=torch.long)
        with torch.inference_mode():
            output = self.model.heads(context, card_row=card_tensor)

        cell_logits = output.cell_logits[0]
        flat = torch.full_like(cell_logits, float("-inf"))
        indices = torch.tensor([y * self.width + x for x, y in cells], device=cell_logits.device)
        flat[indices] = cell_logits[indices]
        cell_index = int(flat.argmax(dim=-1))
        if int(cell_logits.argmax(dim=-1)) not in set(int(value) for value in indices):
            self.stats["cell_raw_argmax_illegal"] += 1
        # The plan/replay boundary is native; delay conditioning below still
        # receives the model-space index that selected this cell.
        cell = event_cell_to_native(cell_index, owner=owner, board_width=self.width)

        # The delay distribution conditions on the cell too, so it has to be
        # read after the cell is resolved rather than from the first pass.
        with torch.inference_mode():
            output = self.model.heads(
                context,
                card_row=card_tensor,
                cell=torch.tensor([cell_index], device=cell_logits.device, dtype=torch.long),
            )
        from native_runner.match_factory import NATIVE_GAMEPLAY_END_TICK
        from native_runner.training.event_delay_constraints import playable_delay_mask
        legal_delays = torch.tensor(playable_delay_mask(
            self.event_config.delay_offset_ms, tick=int(observation.tick),
            end_tick=NATIVE_GAMEPLAY_END_TICK,
        ), dtype=torch.bool, device=output.delay_logits.device)
        if not bool(legal_delays.any()):
            return None
        delay_bin = int(output.delay_logits[0].masked_fill(~legal_delays, float('-inf')).argmax(dim=-1))
        delay_ms = int(self.event_config.delay_offset_ms[min(delay_bin, len(self.event_config.delay_offset_ms) - 1)])
        self.stats["delay_hist"][str(delay_ms)] = self.stats["delay_hist"].get(str(delay_ms), 0) + 1
        return {
            "owner": owner,
            "card_id": int(card_id),
            "hand_index": int(hand_index),
            "cell": (int(cell[0]), int(cell[1])),
            "delay_ms": delay_ms,
            "scheduled_tick": None,  # filled by the loop
            "raw_card_row": int(card_logits.argmax(dim=-1)),
            "raw_cell": (int(cell_logits.argmax(dim=-1)) % self.width, int(cell_logits.argmax(dim=-1)) // self.width),
        }


def frame_digests(value, prefix=""):
    """Per-field sha256 of a tensorized frame, so two runs can be diffed field by field."""

    import dataclasses

    out = {}
    if torch.is_tensor(value):
        out[prefix or "tensor"] = hashlib.sha256(value.detach().to("cpu").numpy().tobytes()).hexdigest()[:16]
    elif dataclasses.is_dataclass(value):
        for field in dataclasses.fields(value):
            out.update(frame_digests(getattr(value, field.name), f"{prefix}.{field.name}" if prefix else field.name))
    elif isinstance(value, dict):
        for key in sorted(value):
            out.update(frame_digests(value[key], f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            out.update(frame_digests(item, f"{prefix}[{index}]"))
    return out

def wait_span(tick: int, plans, event_ticks, last_decision_tick: int, max_advance: int, max_wait: int) -> int:
    """Ticks for one native round-trip, bounded by the next moment we must see.

    Three things can be due: submitting a queued plan at its scheduled tick,
    observing the public event that plan produces (the command applies one tick
    after submission and is visible one tick after that), and the safety wake
    that keeps an idle match moving.  Landing on the earliest of them keeps the
    decision ticks identical to a per-tick loop while replacing thousands of
    round-trips with a few dozen.
    """

    bounds = [int(max_advance), max(1, int(max_wait) - (tick - int(last_decision_tick)))]
    bounds.extend(max(1, int(plan["scheduled_tick"]) - tick) for plan in plans if plan is not None)
    bounds.extend(max(1, int(when) - tick) for when in event_ticks if int(when) > tick)
    return max(1, min(bounds))

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--level-cap", type=int, default=TOURNAMENT_LEVEL)
    parser.add_argument("--card-level-min", type=int, default=TOURNAMENT_LEVEL)
    parser.add_argument(
        "--king-level",
        type=int,
        default=TOURNAMENT_LEVEL,
        help="King Tower level; 0 keeps the template value",
    )
    parser.add_argument("--deck0", default="golem-beatdown")
    parser.add_argument("--deck1", default="pekka-bridge-spam")
    parser.add_argument("--seed", type=int, default=20_260_914)
    parser.add_argument("--max-ticks", type=int, default=5_800)
    parser.add_argument(
        "--max-wait-ticks",
        type=int,
        default=130,
        help="safety wake when no public event arrives (labels treat >120 ticks as WAIT)",
    )
    parser.add_argument("--max-seconds", type=float, default=900.0)
    parser.add_argument("--port", type=int, default=26789)
    parser.add_argument("--env-id", type=int, default=0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--no-mask", action="store_true", help="decode without legality masking (diagnostic)")
    parser.add_argument(
        "--max-advance-ticks",
        type=int,
        default=300,
        help="upper bound on ticks per native round-trip (event stepping)",
    )
    parser.add_argument(
        "--dump-frames",
        action="store_true",
        help="record per-field digests of every tensorized frame into frames.jsonl",
    )
    parser.add_argument(
        "--dump-observations",
        type=int,
        default=0,
        help="also dump this many raw observations into observations.jsonl",
    )
    parser.add_argument(
        "--debug-timeline",
        action="store_true",
        help="write timeline.jsonl with plan submissions and first-seen events",
    )
    parser.add_argument(
        "--no-step-until-event",
        action="store_true",
        help="step the engine per tick instead of landing on the next event",
    )
    add_lightning_target_arguments(parser)
    add_log_target_arguments(parser)
    args = parser.parse_args()
    lightning_constraint = lightning_constraint_from_args(args)
    log_constraint = log_constraint_from_args(args)
    if (lightning_constraint is not None or log_constraint is not None) and args.no_mask:
        parser.error("spell target constraints require legality masking")

    decks = _load_deck_registry()
    for name in (args.deck0, args.deck1):
        if name not in decks:
            raise SystemExit(f"unknown deck {name!r}; known: {sorted(decks)}")
    deck0 = tuple(int(v) for v in decks[args.deck0])
    deck1 = tuple(int(v) for v in decks[args.deck1])

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    match_config = MatchConfig(
        deck0=deck0,
        deck1=deck1,
        seed=args.seed,
        level_cap=args.level_cap,
        minimum_card_level=args.card_level_min,
        king_tower_level=(args.king_level or None),
    )
    (out_dir / "render-match.json").write_text(match_config.to_json(), encoding="utf-8")

    from native_runner.training.event_checkpoint import load_event_checkpoint

    loaded = load_event_checkpoint(args.checkpoint, device=args.device)
    model, state = loaded.model, loaded.payload
    event_config = model.event_config
    if event_config.delay_offset_ms[-1] == 3000:
        args.max_wait_ticks = event_config.max_wait_ticks
    model.eval()
    catalog = model.backbone.catalog
    decoder = Decoder(model, catalog, event_config, args.device)

    bundle = production_semantic_bundle()
    proxies, coordinator = build_resident_training_proxies_v4(
        (args.env_id,),
        host="127.0.0.1",
        port=args.port,
        timeout=90.0,
        step_until_event=not args.no_step_until_event,
    )
    proxy = proxies[0]
    # An explicit ruleset ID (no manifest) is the capture-side contract: the
    # resident engines were built from locally patched probes, so their
    # reported probe hash never matches the asset-pack manifest.
    execution_ruleset_id = hashlib.sha256(
        f"demo-event-selfplay:{args.deck0}:{args.deck1}:{args.seed}".encode("utf-8")
    ).hexdigest()
    environment = BattleEnvV1(
        native=proxy,
        card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
        ruleset_id=execution_ruleset_id,
    )

    trajectory: list[dict] = []
    timeline: list[dict] = []
    observation_dumps: list[dict] = []
    frames_digest: list[dict] = []
    plans: dict[int, dict | None] = {0: None, 1: None}
    ledger = EventExecutionLedger()
    recovery_owners: set[int] = set()
    queued_trajectory: dict[str, dict] = {}
    event_ticks: list[int] = []
    seen_events: set[str] = set()
    counters = {
        "scheduler_version": EVENT_SCHEDULER_VERSION,
        "events": 0,
        "timeout_wakes": 0,
        "installed": 0,
        "executed": 0,
        "decisions_after_gameplay": 0,
        "submissions_after_gameplay": 0,
        "cancelled": 0,
        "cancelled_lightning_target": 0,
        "cancelled_log_target": 0,
        "installed_by_owner": {"0": 0, "1": 0},
        "cancelled_by_owner": {"0": 0, "1": 0},
        "native_steps": 0,
        "executed_by_owner": {"0": 0, "1": 0},
    }
    elixir_ticks = {"samples": 0, "cap": {"0": 0, "1": 0}, "sum": {"0": 0.0, "1": 0.0}}
    elixir_last_sample = -10
    started = time.perf_counter()
    tick = 0
    terminal = False
    result: dict = {}
    try:
        observations, _info = environment.reset(
            match_config=match_config, options={**RESET_OPTIONS, "decision_ticks": POLICY_DECISION_TICKS}
        )
        initial_elixir = {
            owner: next(p.elixir_exact for p in observations[owner].players if p.owner == owner)
            for owner in (0, 1)
        }
        tensorizers = {}
        for owner in (0, 1):
            tensorizer = build_episode_tensorizer_v4(environment.episode_config, actor_owner=owner)
            tensorizer.start_episode(observations[owner], initial_elixir=initial_elixir)
            tensorizers[owner] = tensorizer
        coordinator.start_session()
        perspective = {
            str(owner): {
                "flip_x": bool(tensorizers[owner].perspective.flip_x),
                "flip_y": bool(tensorizers[owner].perspective.flip_y),
                "hand_slot_permutation": list(getattr(tensorizers[owner].perspective, "hand_slot_permutation", ()) or ()),
            }
            for owner in (0, 1)
        }

        tick = int(observations[0].tick)
        last_decision_tick = tick
        last_decision_by_owner = {0: tick, 1: tick}
        while tick < args.max_ticks and time.perf_counter() - started < args.max_seconds:
            commands: dict[int, tuple] = {}
            gameplay_open = policy_actions_open(environment, tick)
            if not gameplay_open:
                plans = {0: None, 1: None}
                recovery_owners.clear()
            for owner in (0, 1):
                plan = plans[owner]
                if plan is not None and tick >= int(plan["scheduled_tick"]):
                    plans[owner] = None
                    recovery_owners.add(owner)
                    if plan.get("kind") == "activate_ability":
                        try:
                            action = ability_action_from_plan(environment, observations[owner], owner, plan,
                                action_id=f"demo:{ledger.submitted}:{owner}")
                        except ValueError:
                            counters["cancelled"] += 1
                            counters["cancelled_by_owner"][str(owner)] += 1
                            continue
                        commands[owner] = (action,)
                        ledger.submit(action)
                        recovery_owners.discard(owner)
                        queued_trajectory[action.action_id] = dict(tick=max(0, tick - 1), actions={
                            str(owner): [dict(kind="activate_ability", source_entity=action.source_entity,
                                              ability_id=action.ability_id, execute_offset_ticks=2)]})
                        continue
                    player = next((item for item in observations[owner].players if item.owner == owner), None)
                    if player is None or int(plan["card_id"]) not in player.hand:
                        counters["cancelled"] += 1
                        continue
                    slot = next(slot for slot, card in native_hand_slots(player) if card == int(plan["card_id"]))
                    mask = environment.action_mask(owner)
                    entry = mask.placement_masks.get(str(slot), {})
                    rows = entry.get("row_major", ())
                    x, y = plan["cell"]
                    if (slot >= len(mask.hand_slots) or not mask.hand_slots[slot]
                        or not mask.kinds.get("play_card", False)
                        or not (0 <= y < len(rows) and 0 <= x < len(rows[y]) and rows[y][x])):
                        counters["cancelled"] += 1
                        continue
                    if not spell_target_allowed(
                        observations[owner], owner, plan["card_id"], plan["cell"],
                        lightning_constraint=lightning_constraint, log_constraint=log_constraint,
                    ):
                        counters["cancelled"] += 1
                        counters["cancelled_by_owner"][str(owner)] += 1
                        spell = "log" if int(plan["card_id"]) == LOG_CARD_ID else "lightning"
                        counters[f"cancelled_{spell}_target"] += 1
                        continue
                    try:
                        action = ActionV1.play(
                            owner,
                            next(slot for slot, card in native_hand_slots(player)
                                 if card == int(plan["card_id"])),
                            plan["cell"],
                            card_id=int(plan["card_id"]),
                            execute_offset_ticks=1,
                            action_id=f"demo:{ledger.submitted}:{owner}",
                        )
                    except Exception:  # noqa: BLE001 - engine rejects a stale plan
                        counters["cancelled"] += 1
                        continue
                    commands[owner] = (action,)
                    ledger.submit(action)
                    counters["submissions_after_gameplay"] += int(not policy_actions_open(environment, tick))
                    recovery_owners.discard(owner)
                    queued_trajectory[action.action_id] = (
                        {
                            "tick": max(0, tick - 1),
                            "actions": {
                                str(owner): [
                                    {
                                        "kind": "play_card",
                                        "card_id": int(plan["card_id"]),
                                        "target_grid": [int(plan["cell"][0]), int(plan["cell"][1])],
                                        "execute_offset_ticks": 2,
                                    }
                                ]
                            },
                        }
                    )
            if not commands:
                event_ticks = [when for when in event_ticks if when > tick]
                span = wait_span(
                    tick,
                    plans.values(),
                    event_ticks,
                    last_decision_tick,
                    args.max_advance_ticks,
                    args.max_wait_ticks,
                )
                if not gameplay_open:
                    span = args.max_advance_ticks
                elif recovery_owners:
                    span = 1
                if gameplay_open:
                    span = min(span, max(1, gameplay_end_tick(environment) - tick))
                commands = {owner: (ActionV1.wait(owner, ticks=span),) for owner in (0, 1)}
            commands = complete_event_commands(commands)
            counters["native_steps"] += 1
            coordinator.begin_round((args.env_id,))
            try:
                observations, _rewards, terms, truncs, _infos = environment.step(
                    commands, record_trace=False, policy_features_only=True
                )
            except BaseException:
                coordinator.abort_round(RuntimeError("headless self-play step failed"))
                raise
            else:
                coordinator.finish_round()
            tick = int(observations[0].tick)
            terminal = bool(terms["__all__"]) or bool(truncs["__all__"])
            outcomes = environment.action_outcomes()
            recovery_owners.update(ledger.reconcile(outcomes))
            for event in outcomes:
                record = queued_trajectory.pop(event.data.get("action_id"), None)
                if record is not None and event.event_type == "action_executed":
                    trajectory.append(record)
            counters.update(ledger.counters())
            counters["executed_by_owner"] = {str(o): n for o, n in ledger.confirmed_by_owner.items()}
            # Sample on every landing that advances at least a few ticks: with
            # batched stepping the loop lands on arbitrary ticks, so a modulo
            # test would almost never fire.
            if tick - elixir_last_sample >= 10:
                elixir_ticks["samples"] += 1
                elixir_last_sample = tick
                for owner in (0, 1):
                    value = next(
                        (float(p.elixir_exact) for p in observations[owner].players if int(p.owner) == owner),
                        None,
                    )
                    if value is None:
                        continue
                    key = str(owner)
                    elixir_ticks["sum"][key] += value
                    elixir_ticks["cap"][key] += int(value >= 9.5)

            fresh = [dict(owner=e.owner, kind="activate_ability", source_entity=e.entity_id)
                     for e in ledger.ability_events]
            counters["confirmed_abilities"] = ledger.confirmed_abilities
            for owner in (0, 1):
                for item in environment.deployment_timing_events(owner):
                    event_id = str(item.get("nativeEventId", ""))
                    if not event_id or event_id in seen_events:
                        continue
                    if str(item.get("kind")) != "card_play":
                        continue
                    seen_events.add(event_id)
                    fresh.append(item)
            if terminal:
                break
            if not policy_actions_open(environment, tick):
                continue
            timed_out = {o for o in (0, 1) if tick - last_decision_by_owner[o] >= args.max_wait_ticks}
            decision_owners = {0, 1} if fresh else timed_out | recovery_owners
            if fresh:
                counters["events"] += 1
                if args.debug_timeline:
                    timeline.append({"kind": "event", "tick": tick,
                                     "owners": sorted({int(e.get("owner", -1)) for e in fresh}),
                                     "cards": sorted({int(e.get("cardId", -1)) for e in fresh})})
            elif timed_out:
                # Safety wake: the event contract only wakes on public plays, so
                # an event-driven self-play match would deadlock at kickoff.
                counters["timeout_wakes"] += 1
            if not decision_owners:
                continue

            for owner in (0, 1):
                if owner not in decision_owners:
                    continue
                if not environment.action_mask(owner).reasons.get("decision_ready", True):
                    continue
                trigger = "event" if fresh else "recovery" if owner in recovery_owners else "wake"
                recovery_owners.discard(owner)
                last_decision_by_owner[owner] = tick
                last_decision_tick = min(last_decision_by_owner.values())
                frame = tensorizers[owner].tensorize(observations[owner], validate=False)
                if args.dump_frames:
                    flag = frame.events.mask
                    frames_digest.append(
                        {
                            "tick": tick, "owner": owner,
                            "events_count": int(flag.sum()),
                            "events_capacity": int(flag.shape[-1]),
                            "fields": frame_digests(frame),
                        }
                    )
                if args.no_mask:
                    options, reason = {}, None
                else:
                    options, reason = legal_play_options(
                        environment, observations[owner], owner,
                        lightning_constraint=lightning_constraint, log_constraint=log_constraint,
                    )
                legal = None
                if getattr(model.config, "ability_actions", False):
                    if args.no_mask:
                        raise ValueError("ability decoding always requires native legality")
                    legal = legal_event_actions(environment, observations[owner], frame, owner,
                        catalog=catalog, card_count=model.card_count, cell_count=model.cell_count,
                        board_width=event_config.board_width, board_height=event_config.board_height,
                        delay_offset_ms=event_config.delay_offset_ms, device=args.device,
                        lightning_constraint=lightning_constraint, log_constraint=log_constraint)
                decision = decoder(frame, observations[owner], owner, options, reason, trigger, legal=legal)
                counters["decisions_after_gameplay"] += int(not policy_actions_open(environment, tick))
                if decision is None:
                    plans[owner] = None
                    continue
                decision["scheduled_tick"] = (tick + decision.get("delay_ms", 0) // TICK_MS if decision.get("kind") == "activate_ability"
                                              else tick + max(1, math.ceil(decision["delay_ms"] / TICK_MS)))
                if plans[owner] is not None:
                    counters["cancelled"] += 1
                    counters["cancelled_by_owner"][str(owner)] += 1
                plans[owner] = decision
                # Measured on this engine: a plan submitted at its scheduled
                # tick surfaces as a public deployment event one tick later.
                event_ticks.append(int(decision["scheduled_tick"]) + 1)
                if args.debug_timeline:
                    raw = environment.raw_observation
                    player = next((x for x in observations[owner].players if int(x.owner) == owner), None)
                    entry = {
                        "kind": "submit", "tick": tick, "owner": owner,
                        "card": decision.get("card_id"), "scheduled_tick": decision["scheduled_tick"],
                        "action_kind": decision.get("kind", "play_card"),
                        "source_entity": decision.get("source_entity"),
                        "delay_ms": decision["delay_ms"],
                        "elixir": round(float(player.elixir_exact), 4) if player else None,
                        "towers": sorted((int(t.owner), t.tower_kind, int(t.hitpoints)) for t in observations[owner].towers),
                        "raw_sha": hashlib.sha256(
                            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest()[:16],
                    }
                    timeline.append(entry)
                    if args.dump_observations and len(observation_dumps) < args.dump_observations:
                        observation_dumps.append(
                            {"tick": tick, "owner": owner, "raw": raw}
                        )
                counters["installed"] += 1
                counters["installed_by_owner"][str(owner)] += 1
            if terminal:
                break

        terminal_obs = observations[0].terminal
        result = {
            "terminal": bool(terminal),
            "tick": tick,
            "ended": bool(getattr(terminal_obs, "ended", False)),
            "winner": getattr(terminal_obs, "winner", None),
            "reason": getattr(terminal_obs, "reason", None),
            "result_by_owner": getattr(terminal_obs, "result_by_owner", None),
            "crowns": {int(p.owner): int(p.crowns) for p in observations[0].players},
            "towers": [
                {"owner": int(t.owner), "kind": t.tower_kind, "hp": int(t.hitpoints), "active": bool(t.active)}
                for t in observations[0].towers
            ],
        }
    finally:
        try:
            coordinator.close_session()
        except Exception:  # noqa: BLE001 - best effort teardown
            pass

    with (out_dir / "trajectory.jsonl").open("w", encoding="utf-8") as handle:
        for row in trajectory:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    stats = {
        "checkpoint": str(args.checkpoint),
        "checkpoint_steps": state.get("steps"),
        "decks": {"0": args.deck0, "1": args.deck1},
        "deck_ids": {"0": list(deck0), "1": list(deck1)},
        "seed": args.seed,
        "levels": {
            "level_cap": args.level_cap,
            "minimum_card_level": args.card_level_min,
            "king_tower_level": args.king_level or None,
        },
        "masking": not args.no_mask,
        "action_constraints": action_constraints_contract(lightning_constraint, log_constraint),
        "perspective": perspective,
        "counters": counters,
        "decode_stats": decoder.stats,
        "per_owner": {
            str(owner): {
                **{k: v for k, v in bucket.items() if k != "elixir_sum"},
                "elixir_mean_at_decision": round(bucket["elixir_sum"] / max(1, bucket["decisions"]), 3),
                "elixir_cap_rate_at_decision": round(bucket["elixir_cap"] / max(1, bucket["decisions"]), 3),
            }
            for owner, bucket in decoder.per_owner.items()
        },
        "elixir_time": {
            "samples": elixir_ticks["samples"],
            "mean": {
                key: round(elixir_ticks["sum"][key] / max(1, elixir_ticks["samples"]), 3) for key in ("0", "1")
            },
            "at_cap_rate": {
                key: round(elixir_ticks["cap"][key] / max(1, elixir_ticks["samples"]), 3) for key in ("0", "1")
            },
        },
        "scheduled_plays": len(trajectory),
        "step_until_event": not args.no_step_until_event,
        "max_advance_ticks": args.max_advance_ticks,
        "wall_seconds": round(time.perf_counter() - started, 1),
        "result": result,
    }
    if args.debug_timeline:
        with (out_dir / "timeline.jsonl").open("w", encoding="utf-8") as handle:
            for row in timeline:
                handle.write(json.dumps(row) + "\n")
    if frames_digest:
        with (out_dir / "frames.jsonl").open("w", encoding="utf-8") as handle:
            for row in frames_digest:
                handle.write(json.dumps(row) + "\n")
    if observation_dumps:
        with (out_dir / "observations.jsonl").open("w", encoding="utf-8") as handle:
            for row in observation_dumps:
                handle.write(json.dumps(row) + "\n")
    (out_dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: stats[k] for k in ("scheduled_plays", "counters", "result", "wall_seconds")}, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
