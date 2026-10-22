"""Learner-side overflow diagnostics, separated from true match outcomes."""
from collections import defaultdict
import json


def summarize_overflow(counters):
    groups = defaultdict(lambda: dict(lanes=0, waste_elixir=0., penalty=0., capped=0,
        wins=0, draws=0, losses=0, first_play_seconds_sum=0., first_play_count=0,
        single_elixir_zero_plays=0, measured_ticks=0, skipped_terminal_ticks=0))
    for counter in counters:
        report = counter.get("elixir_overflow")
        if report is None:
            continue
        match = counter["mixed_opponents"]
        for owner in counter["recorded_owners"]:
            data = report["owners"][str(owner)]
            outcome = counter["results"][str(owner)]
            for multiplier in ("all", "1", "2", "3"):
                phase = data if multiplier == "all" else data["by_multiplier"][multiplier]
                ticks = data["measured_ticks"] if multiplier == "all" else phase["ticks"]
                if multiplier != "all" and not ticks:
                    continue
                key = match["owner_deck_ids"][owner]+":"+multiplier
                g = groups[key]
                g["lanes"] += 1
                g["waste_elixir"] += phase["waste_raw"]/10_000
                g["penalty"] += phase["penalty"]
                g["capped"] += int(data["cap_reached"])
                g["wins" if outcome > .5 else "losses" if outcome < -.5 else "draws"] += 1
                g["single_elixir_zero_plays"] += int(data["single_elixir_zero_plays"])
                g["measured_ticks"] += ticks
                g["skipped_terminal_ticks"] += data["skipped_terminal_ticks"] if multiplier == "all" else 0
                if data["first_play_seconds"] is not None:
                    g["first_play_seconds_sum"] += data["first_play_seconds"]
                    g["first_play_count"] += 1
    return derived(dict(groups))


def derived(groups):
    for g in groups.values():
        g["cap_rate"] = g["capped"]/g["lanes"]
        g["true_win_rate"] = g["wins"]/g["lanes"]
        g["true_score_rate"] = (g["wins"]+.5*g["draws"])/g["lanes"]
        g["single_elixir_zero_play_rate"] = g["single_elixir_zero_plays"]/g["lanes"]
        g["mean_first_play_seconds"] = (g["first_play_seconds_sum"]/g["first_play_count"]
                                         if g["first_play_count"] else None)
    return groups


def recent_overflow_summary(root, config, window=10):
    rows = []
    for path in sorted(root.glob("iteration-*/success.json"), reverse=True):
        success = json.loads(path.read_text())
        if success.get("reward_config") != config:
            continue
        validation = json.loads((path.parent/"native-validation.json").read_text())
        rows.append((success["iteration"], validation["elixir_overflow"]))
        if len(rows) == window:
            break
    groups = {}
    fields = ("lanes", "waste_elixir", "penalty", "capped", "wins", "draws", "losses",
              "first_play_seconds_sum", "first_play_count", "single_elixir_zero_plays",
              "measured_ticks", "skipped_terminal_ticks")
    for _, summary in rows:
        for key, row in summary.items():
            total = groups.setdefault(key, dict.fromkeys(fields, 0))
            for field in fields:
                total[field] += row[field]
    return dict(reward_config=config, requested_rounds=window, available_rounds=len(rows),
                iterations=sorted(i for i, _ in rows), by_deck_multiplier=derived(groups))
