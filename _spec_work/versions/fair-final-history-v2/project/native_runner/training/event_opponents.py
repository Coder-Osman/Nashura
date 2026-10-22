"""Deterministic, immutable mixed-opponent round plans (standard library only)."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random

VERSION = "mixed-history.v1"
QUOTAS = {"34": (16, 8), "35": (16, 8), "37": (16, 8), "39": (12, 6), "41": (12, 6)}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def checkpoint_records(root):
    records = []
    for path in sorted(Path(root).glob("iteration-*/success.json")):
        record = json.loads(path.read_text())
        iteration = record["iteration"]
        if path.parent.name != f"iteration-{iteration:06d}":
            raise ValueError("checkpoint iteration identity mismatch")
        sha = Path(record["checkpoint"]).stem
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid checkpoint SHA")
        records.append(dict(iteration=iteration, sha256=sha))
    return records


def select_pool(records, current_iteration, current_sha):
    by_round = {r["iteration"]: r for r in records}
    fixed = [dict(by_round[i]) for i in (600, 645) if i in by_round]
    if len(fixed) != 2:
        raise ValueError("complete fixed checkpoints 600 and 645 are required")
    seen = {current_sha}
    recent = []
    for row in sorted(records, key=lambda r: r["iteration"], reverse=True):
        i, sha = row["iteration"], row["sha256"]
        if i >= 650 and i % 25 == 0 and i <= current_iteration - 10 and sha not in seen:
            recent.append(dict(row))
            seen.add(sha)
            if len(recent) == 4:
                break
    if not recent:
        raise ValueError("no eligible complete recent checkpoint; refusing fallback")
    return dict(fixed=fixed, recent=recent)


def build_plan(*, iteration, seed_base, current_sha, pool, fleet):
    if [r['iteration'] for r in pool['fixed']] != [600, 645]:
        raise ValueError("fixed historical pool must be 600 and 645")
    recent = pool['recent']
    if not 1 <= len(recent) <= 4 or len({r['sha256'] for r in recent}) != len(recent) or any(
        r['iteration'] < 650 or r['iteration'] % 25 or r['iteration'] > iteration-11 for r in recent
    ):
        raise ValueError("invalid recent historical pool")
    if any(r['sha256'] == current_sha for rows in pool.values() for r in rows):
        raise ValueError("historical pool aliases current model")
    if set(fleet) != set(QUOTAS):
        raise ValueError("mixed policy requires the configured five-host fleet")
    rng = random.Random(digest([VERSION, seed_base, iteration]))
    pairs = [("fixed", row) for row in pool["fixed"] for _ in range(3)]
    if 12 % len(pool["recent"]):
        raise ValueError("recent pool cannot be evenly paired")
    pairs += [("recent", row) for row in pool["recent"] for _ in range(12 // len(pool["recent"]))]
    rng.shuffle(pairs)
    matches = []
    used_seeds = set()
    def seed():
        while True:
            value = rng.randrange(2**32)
            if value not in used_seeds:
                used_seeds.add(value)
                return value
    pair_index = 0
    for host, (self_count, history_count) in QUOTAS.items():
        ports = sorted(fleet[host]["ports"])
        if len(ports) != self_count + history_count or len(set(ports)) != len(ports):
            raise ValueError("collector port quota mismatch")
        jobs = [dict(kind="self", learner_owner=None, seed=seed(), pair_id=None,
                     opponent=None, owner_sha256=[current_sha, current_sha]) for _ in range(self_count)]
        for _ in range(history_count // 2):
            category, opponent = pairs[pair_index]
            pair_seed = seed()
            for owner in (0, 1):
                identities = [opponent["sha256"], opponent["sha256"]]
                identities[owner] = current_sha
                jobs.append(dict(kind=category, learner_owner=owner, seed=pair_seed,
                                 pair_id=f"{iteration}-pair-{pair_index:02d}", opponent=opponent,
                                 owner_sha256=identities))
            pair_index += 1
        rng.shuffle(jobs)
        for port, job in zip(ports, jobs):
            matches.append(dict(job, host=host, port=port, game_id=f"{iteration}-{host}-{port}"))
    plan = dict(version=VERSION, iteration=iteration, current_iteration=iteration-1,
                seed_base=seed_base, current_sha256=current_sha, pool=pool, matches=matches,
                expected_games=108, expected_player_lanes=180)
    return dict(plan, sha256=digest(plan))


def validate_plan(plan):
    if plan.get("version") == "mixed-deck.v1":
        from .mixed_decks import validate_plan as validate_mixed
        return validate_mixed(plan)
    body = {k: v for k, v in plan.items() if k != "sha256"}
    if plan.get("version") != VERSION or plan.get("sha256") != digest(body):
        raise ValueError("mixed round plan SHA mismatch")
    fleet = {h: {"ports": [m["port"] for m in plan["matches"] if m["host"] == h]} for h in QUOTAS}
    expected = build_plan(iteration=plan["iteration"], seed_base=plan["seed_base"],
                          current_sha=plan["current_sha256"], pool=plan["pool"], fleet=fleet)
    if expected != plan:
        raise ValueError("mixed round plan allocation mismatch")
    return plan


def freeze_plan(path, **kwargs):
    path = Path(path)
    if path.exists():
        plan = validate_plan(json.loads(path.read_text()))
        for key, wanted in (("iteration", kwargs["iteration"]), ("seed_base", kwargs["seed_base"]),
                            ("current_sha256", kwargs["current_sha"])):
            if plan[key] != wanted:
                raise ValueError("persisted round plan identity mismatch")
        for key in ("start_iteration", "action_constraints", "deck_policy", "reward_config", "allocation_policy"):
            if key in kwargs and plan.get(key) != kwargs[key]:
                raise ValueError("persisted mixed-deck curriculum changed")
        if {(m["host"], m["port"]) for m in plan["matches"]} != {
            (h, p) for h, config in kwargs["fleet"].items() for p in config["ports"]
        }:
            raise ValueError("persisted plan collector ports changed")
        return plan
    builder = kwargs.pop("builder", build_plan)
    plan = builder(**kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(plan, stream, indent=2)
    return plan


def match_metadata(plan, match):
    extra = {k: plan[k] for k in ("mixed_round", "phase", "learning_weights", "rules", "action_constraints", "deck_policy", "reward_config", "allocation_policy")
             if k in plan}
    return dict(version=plan["version"], plan_sha256=plan["sha256"], **extra, **match)


def validate_shard_ownership(shard, metadata):
    if shard.manifest.get("contract", {}).get("reward_config") != metadata.get("reward_config"):
        raise ValueError("shard reward configuration differs from the frozen plan")
    if shard.manifest.get("contract", {}).get("mixed_opponents") != metadata:
        raise ValueError("shard mixed opponent identity mismatch")
    owners = {0, 1} if metadata["kind"] == "self" else {metadata["learner_owner"]}
    if {r.owner for r in shard.records} != owners:
        raise ValueError("frozen opponent entered learner shard or learner lane missing")
    if any(r.match_id != metadata["game_id"] or
           r.behavior_checkpoint_sha256 != metadata["owner_sha256"][r.owner] for r in shard.records):
        raise ValueError("learner row behavior identity disagrees with match plan")
    if metadata["version"] == "mixed-deck.v1":
        if shard.manifest["contract"].get("action_constraints") != metadata["action_constraints"]:
            raise ValueError("mixed-deck action constraints mismatch")
        if any(list(r.deck) != metadata["owner_decks"][r.owner] or
               list(r.opponent_deck) != metadata["owner_decks"][1-r.owner] for r in shard.records):
            raise ValueError("mixed-deck learner/opponent deck mismatch")


def result_statistics(counters):
    groups = defaultdict(lambda: dict(games=0, wins=0, draws=0, losses=0, seconds=0.))
    for counter in counters:
        match = counter["mixed_opponents"]
        if match["version"] == "mixed-deck.v1":
            owners = (0, 1) if match["kind"] == "self" else (match["learner_owner"],)
            for side in owners:
                key = f"{match['owner_deck_ids'][side]}:{match['owner_deck_ids'][1-side]}:{match['owner_sha256'][1-side]}"
                reward = float(counter["results"][str(side)])
                group = groups[key]
                group["games"] += 1
                group["wins" if reward > .5 else "losses" if reward < -.5 else "draws"] += 1
                group["seconds"] += counter["elapsed_s"]
                group["score_rate"] = (group["wins"] + .5 * group["draws"]) / group["games"]
            continue
        owner = match["learner_owner"]
        opponent = match["opponent"]
        key = "self" if owner is None else f"{opponent['iteration']}:{opponent['sha256']}:owner{owner}"
        reward = float(counter["results"][str(0 if owner is None else owner)])
        group = groups[key]
        group["games"] += 1
        # This run's terminal margin is strictly below .5. A draw can have a
        # nonzero margin; its reward sign must not turn it into a win/loss.
        group["wins" if reward > .5 else "losses" if reward < -.5 else "draws"] += 1
        group["seconds"] += counter["elapsed_s"]
    return dict(groups)
