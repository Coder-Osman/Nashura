"""Versioned mixed-deck curricula and reproducible match allocation."""
from __future__ import annotations

from collections import Counter
import random

from .event_opponents import digest, QUOTAS

VERSION = "mixed-deck.v1"
SCALED_ALLOCATION = "scaled-five-deck.v1"
DECKS = {
    "golem": (26000009, 26000048, 26000043, 26000015, 26000039, 28000007, 28000011, 27000009),
    "giant": (26000003, 26000018, 26000014, 26000015, 26000013, 26000039, 28000000, 28000008),
    "hog": (26000021, 26000038, 26000014, 26000010, 26000030, 27000000, 28000000, 28000011),
    "royal-giant": (26000024, 26000000, 26000014, 26000039, 26000010, 27000000, 28000000, 28000011),
}
DECK_IDS = tuple(DECKS)
RECRUITS_HOGS_POLICY = "five-deck-recruits-hogs.v1"
DECKS["recruits-hogs"] = (26000047, 26000059, 26000057, 26000052,
                         27000012, 28000000, 28000001, 28000015)
FIVE_DECK_IDS = tuple(DECKS)
VARIANTS = {
    name + "-heldout": tuple(new if card == old else card for card in DECKS[name])
    for name, old, new in (("golem", 26000043, 26000018), ("giant", 26000013, 26000000),
                           ("hog", 26000038, 26000000), ("royal-giant", 26000000, 26000018))
}
RULES = dict(card_form="base", level=11, tower="princess", minimum_delay_ms=100)


def deck_ids(deck_policy=None):
    # The original registry remains reproducible for archived plans/evaluations.
    if deck_policy is None:
        return DECK_IDS
    if deck_policy == RECRUITS_HOGS_POLICY:
        return FIVE_DECK_IDS
    raise ValueError("unknown mixed-deck policy")


def stage(round_number, deck_policy=None):
    ids = deck_ids(deck_policy)
    if type(round_number) is not int or round_number < 1:
        raise ValueError("mixed round must be a positive integer")
    if deck_policy == RECRUITS_HOGS_POLICY:
        return 3, dict.fromkeys(ids, 1 / len(ids))
    if round_number > 100:
        raise ValueError("mixed round must be within 1..100")
    index, weights = ((1, (.6, .2, .1, .1)) if round_number <= 20 else
                      (2, (.4, .2, .2, .2)) if round_number <= 60 else (3, (.25,) * 4))
    return index, dict(zip(DECK_IDS, weights))


def select_pool(records, *, round_number, start_iteration, baseline, fixed):
    if [r["iteration"] for r in fixed] not in ([], [600, 645]):
        raise ValueError("fixed pool must be baseline-only or legacy 600/645")
    recent = []
    seen = {baseline["sha256"]}
    for row in sorted(records, key=lambda r: r["iteration"], reverse=True):
        mixed_round = row["iteration"] - start_iteration
        if 1 <= mixed_round <= round_number - 11 and row["sha256"] not in seen:
            recent.append(dict(row, mixed_round=mixed_round))
            seen.add(row["sha256"])
            if len(recent) == 2:
                break
    return dict(fixed=[dict(baseline, label="B0"), *fixed], recent=recent)


def build_plan(*, iteration, seed_base, current_sha, pool, fleet, start_iteration,
               action_constraints, deck_policy=None, reward_config=None, allocation_policy=None):
    from .event_rewards import reward_config as checked_rewards
    reward_config = checked_rewards(reward_config)
    round_number = iteration - start_iteration
    phase, weights = stage(round_number, deck_policy)
    ids = deck_ids(deck_policy)
    if set(fleet) != set(QUOTAS):
        raise ValueError("mixed-deck requires the configured five-host fleet")
    quotas = QUOTAS
    games, pair_count, fixed_pairs = 108, 18, 6
    if allocation_policy is not None:
        if allocation_policy != SCALED_ALLOCATION or deck_policy != RECRUITS_HOGS_POLICY:
            raise ValueError("unknown scaled mixed-deck allocation")
        sizes = {h: len(fleet[h]["ports"]) for h in QUOTAS}
        games = sum(sizes.values())
        if games % 36 or any(n < 6 or n % 6 for n in sizes.values()):
            raise ValueError("scaled allocation requires 36-game blocks and six-port host blocks")
        quotas = {h: (2*n//3, n//3) for h, n in sizes.items()}
        pair_count, fixed_pairs = games//6, games//18
    if action_constraints.get("delay") != {"minimum_ms": 100}:
        raise ValueError("mixed-deck requires the 100ms action contract")
    fixed, recent = pool["fixed"], pool["recent"]
    if allocation_policy is not None and (len(fixed) != 1 or len(recent) != 2):
        raise ValueError("scaled allocation requires one baseline and two recent opponents")
    if (len(fixed) not in (1, 3) or fixed[0].get("label") != "B0" or
            fixed[0]["iteration"] != start_iteration or
            [r["iteration"] for r in fixed[1:]] not in ([], [600, 645])):
        raise ValueError("invalid mixed-deck baseline/fixed pool")
    if (len(recent) > 2 or len({r["sha256"] for r in recent}) != len(recent) or
            any(r["mixed_round"] != r["iteration"] - start_iteration or
                not 1 <= r["mixed_round"] <= round_number - 11 for r in recent)):
        raise ValueError("invalid mixed-deck recent pool")
    for sha in [current_sha, *(r["sha256"] for r in fixed + recent)]:
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid weight SHA")
    identity = [VERSION, seed_base, iteration]
    if deck_policy is not None:
        identity.append(deck_policy)
    if allocation_policy is not None:
        identity += [allocation_policy, games]
    rng = random.Random(digest(identity))
    used_seeds = set()

    def seed():
        while True:
            value = rng.randrange(2**32)
            if value not in used_seeds:
                used_seeds.add(value)
                return value

    def job(kind, decks, identities, learner_owner, opponent, pair_id, pair_seed):
        return dict(kind=kind, owner_deck_ids=list(decks),
                    owner_decks=[list(DECKS[d]) for d in decks], owner_sha256=identities,
                    learner_owner=learner_owner, opponent=opponent, pair_id=pair_id, seed=pair_seed)

    # Five decks: three games per ordered pair except three rotating mirrors
    # have two. Those decks receive four history pairs; the other two receive
    # three. Exactly 36 learner lanes per deck, with balanced owners.
    self_jobs = []
    history_start = (round_number - 1) % len(ids)
    reduced_mirrors = {ids[(history_start + k) % len(ids)] for k in range(3)}
    self_counts = None
    if allocation_policy is not None:
        # Equal row/column degrees leave exactly one lane per owner for each history pair.
        history_degrees = Counter(ids[(k + history_start) % len(ids)] for k in range(pair_count))
        self_degrees = {d: pair_count - history_degrees[d] for d in ids}
        base, remainder = divmod(min(self_degrees.values()), len(ids))
        offsets = {(1 + (round_number - 1 + k) % (len(ids)-1)) for k in range(remainder)}
        self_counts = {(a, b): base + int((j-i) % len(ids) in offsets)
                       + int(a == b and self_degrees[a] > min(self_degrees.values()))
                       for i, a in enumerate(ids) for j, b in enumerate(ids)}
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            count = (4 + int((i + j) % 2 == (round_number - 1) % 2)
                     if deck_policy is None else 3 - int(a == b and a in reduced_mirrors))
            if self_counts is not None:
                count = self_counts[a, b]
            for _ in range(count):
                self_jobs.append(job("self", (a, b), [current_sha] * 2, None, None, None, seed()))
    rng.shuffle(self_jobs)
    pairs = []
    for index in range(pair_count):
        learner_deck = ids[(index + (2 * (round_number - 1) if deck_policy is None
                                    else history_start)) % len(ids)]
        if index < fixed_pairs:
            opponent, opponent_deck, kind = fixed[index // (fixed_pairs // len(fixed))], "golem", "fixed"
        elif recent:
            k = index - fixed_pairs
            opponent = recent[((k // 4 + k % 2 if deck_policy is None else k)
                               + round_number - 1) % len(recent)]
            opponent_deck = ids[k % 4] if deck_policy is None else ids[(2 * k + 2 * history_start) % len(ids)]
            kind = "recent"
        else:
            opponent, opponent_deck, kind = fixed[0], "golem", "baseline-fallback"
        pair_seed = seed()
        pair = []
        for owner in (0, 1):
            decks, identities = [opponent_deck] * 2, [opponent["sha256"]] * 2
            decks[owner], identities[owner] = learner_deck, current_sha
            pair.append(job(kind, decks, identities, owner, opponent,
                            f"{iteration}-pair-{index:02d}", pair_seed))
        pairs.append(pair)
    rng.shuffle(pairs)
    matches, offset, pair_offset = [], 0, 0
    for host, (self_count, history_count) in quotas.items():
        ports = sorted(fleet[host]["ports"])
        if (len(ports) != self_count + history_count or len(set(ports)) != len(ports)
                or any(type(p) is not int or not 1 <= p <= 65535 for p in ports)):
            raise ValueError("mixed-deck port quota mismatch")
        jobs = self_jobs[offset:offset + self_count]
        jobs += [j for pair in pairs[pair_offset:pair_offset + history_count // 2] for j in pair]
        offset, pair_offset = offset + self_count, pair_offset + history_count // 2
        rng.shuffle(jobs)
        matches.extend(dict(j, host=host, port=port, game_id=f"{iteration}-{host}-{port}")
                       for port, j in zip(ports, jobs))
    plan = dict(version=VERSION, iteration=iteration, current_iteration=iteration - 1,
                start_iteration=start_iteration, mixed_round=round_number, phase=phase,
                learning_weights=weights, rules=RULES, action_constraints=action_constraints,
                seed_base=seed_base, current_sha256=current_sha, pool=pool, matches=matches,
                expected_games=games, expected_player_lanes=games*5//3, recent_fallback=not bool(recent))
    if allocation_policy is not None:
        plan["allocation_policy"] = allocation_policy
    if deck_policy is not None:
        plan["deck_policy"] = deck_policy
    if reward_config is not None:
        plan["reward_config"] = reward_config
    return dict(plan, sha256=digest(plan))


def validate_plan(plan):
    body = {k: v for k, v in plan.items() if k != "sha256"}
    if plan.get("version") != VERSION or plan.get("sha256") != digest(body):
        raise ValueError("mixed-deck plan SHA mismatch")
    fleet = {h: dict(ports=[m["port"] for m in plan["matches"] if m["host"] == h]) for h in QUOTAS}
    expected = build_plan(iteration=plan["iteration"], seed_base=plan["seed_base"],
                          current_sha=plan["current_sha256"], pool=plan["pool"], fleet=fleet,
                          start_iteration=plan["start_iteration"], action_constraints=plan["action_constraints"],
                          deck_policy=plan.get("deck_policy"), reward_config=plan.get("reward_config"),
                          allocation_policy=plan.get("allocation_policy"))
    if plan != expected:
        raise ValueError("mixed-deck allocation/phase/constraints mismatch")
    return plan


def row_counts(lanes, expected_decks=DECK_IDS):
    counts = Counter()
    for lane in lanes:
        counts[lane.deck_id] += lane.time_steps
    if set(counts) != set(expected_decks) or any(n <= 0 for n in counts.values()):
        raise ValueError("every mixed deck requires valid learner decisions")
    return dict(counts)
