"""League deck registry: three archetypes for segment-reward co-training.

Each specialist policy owns exactly one deck. Matchups are scheduled round
robin so every pair meets with both sides. Card IDs use the Null's Royale
catalog (see native_runner/data/competitive/card_support.json).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HOG_CYCLE_DECK = (
    26000021,  # Hog Rider
    26000038,  # Ice Golem (IceGolemite)
    26000014,  # Musketeer
    26000010,  # Skeletons
    28000000,  # Fireball
    28000011,  # The Log
    27000000,  # Cannon
    26000030,  # Ice Spirit
)

GOLEM_DECK = (
    26000009,  # Golem
    26000048,  # Dark Witch
    26000043,  # Elite Barbarians (AngryBarbarians)
    26000015,  # Baby Dragon
    26000039,  # Mega Minion
    28000007,  # Lightning
    28000011,  # The Log
    27000009,  # Tombstone
)

PEKKA_BRIDGE_SPAM_DECK = (
    26000004,  # P.E.K.K.A
    26000036,  # Battle Ram
    26000046,  # Bandit (Assassin)
    26000050,  # Royal Ghost (Ghost)
    26000042,  # Electro Wizard
    28000009,  # Poison
    28000008,  # Zap
    26000005,  # Minions
)

LAVA_HOUND_DECK = (
    26000029,  # Lava Hound (win condition)
    26000006,  # Balloon (secondary win condition)
    26000039,  # Mega Minion
    27000009,  # Tombstone
    26000080,  # Skeleton Dragons
    28000000,  # Fireball
    28000008,  # Zap
    26000049,  # Bats
)

LEAGUE_DECKS: dict[str, tuple[int, ...]] = {
    "hog-cycle": HOG_CYCLE_DECK,
    "golem-beatdown": GOLEM_DECK,
    "pekka-bridge-spam": PEKKA_BRIDGE_SPAM_DECK,
}

# RL league (SELFPLAY_RL_ARCHITECTURE.md): heavy ground beatdown / air beatdown /
# ground control-pressure. Deliberately a *separate* registry: POLICY_NAMES is
# derived from LEAGUE_DECKS, so adding a deck there would silently change the
# existing IL league from three policies to four.
RL_LEAGUE_DECKS: dict[str, tuple[int, ...]] = {
    "golem-beatdown": GOLEM_DECK,
    "lava-hound": LAVA_HOUND_DECK,
    "pekka-bridge-spam": PEKKA_BRIDGE_SPAM_DECK,
}

# Round-robin pairings: (side-A policy, side-B policy); every ordered pair of
# distinct specialists appears once so both sides of each matchup are learned.
LEAGUE_MATCHUPS: tuple[tuple[str, str], ...] = (
    ("hog-cycle", "golem-beatdown"),
    ("golem-beatdown", "hog-cycle"),
    ("hog-cycle", "pekka-bridge-spam"),
    ("pekka-bridge-spam", "hog-cycle"),
    ("golem-beatdown", "pekka-bridge-spam"),
    ("pekka-bridge-spam", "golem-beatdown"),
)

# Every ordered pair of distinct RL specialists, so both sides of each matchup
# are learned by the specialist that owns that side.
RL_LEAGUE_MATCHUPS: tuple[tuple[str, str], ...] = (
    ("golem-beatdown", "lava-hound"),
    ("lava-hound", "golem-beatdown"),
    ("golem-beatdown", "pekka-bridge-spam"),
    ("pekka-bridge-spam", "golem-beatdown"),
    ("lava-hound", "pekka-bridge-spam"),
    ("pekka-bridge-spam", "lava-hound"),
)


# First RL run: one specialist playing itself, to validate the training loop
# before adding opponents. Mirrors are deliberate here (unlike RL_LEAGUE_MATCHUPS,
# which pairs distinct specialists) because a self-play smoke test needs both
# sides to be the same policy.
SELFPLAY_MATCHUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "golem-beatdown": (("golem-beatdown", "golem-beatdown"),),
}


def _validate(decks: dict[str, tuple[int, ...]]) -> None:
    from native_runner.competitive_data import load_competitive_data

    supported = {int(item["card_id"]) for item in load_competitive_data("card_support")["supported_cards"]}
    for name, deck in decks.items():
        if len(deck) != 8 or len(set(deck)) != 8:
            raise ValueError(f"league deck {name} must contain eight unique cards")
        missing = [card_id for card_id in deck if card_id not in supported]
        if missing:
            raise ValueError(f"league deck {name} uses unsupported cards: {missing}")


def validate_league_decks() -> None:
    """Fail fast when a league card falls outside the supported catalog."""

    _validate(LEAGUE_DECKS)
    _validate(RL_LEAGUE_DECKS)


def _validate_matchups(
    matchups: tuple[tuple[str, str], ...],
    decks: dict[str, tuple[int, ...]],
    *,
    allow_mirrors: bool = False,
) -> None:
    for side_a, side_b in matchups:
        for name in (side_a, side_b):
            if name not in decks:
                raise ValueError(f"matchup refers to unknown specialist {name!r}")
        if side_a == side_b and not allow_mirrors:
            raise ValueError(f"matchup {side_a} vs {side_b} is a mirror; use a snapshot pool instead")


if __name__ == "__main__":
    validate_league_decks()
    _validate_matchups(LEAGUE_MATCHUPS, LEAGUE_DECKS)
    _validate_matchups(RL_LEAGUE_MATCHUPS, RL_LEAGUE_DECKS)
    for name, matchups in SELFPLAY_MATCHUPS.items():
        if name not in RL_LEAGUE_DECKS:
            raise ValueError(f"self-play entry {name!r} is not an RL specialist")
        _validate_matchups(matchups, RL_LEAGUE_DECKS, allow_mirrors=True)
    for name, deck in LEAGUE_DECKS.items():
        print(f"il  {name:20s} {list(deck)}")
    for name, deck in RL_LEAGUE_DECKS.items():
        print(f"rl  {name:20s} {list(deck)}")
    print("il  matchups:", LEAGUE_MATCHUPS)
    print("rl  matchups:", RL_LEAGUE_MATCHUPS)
