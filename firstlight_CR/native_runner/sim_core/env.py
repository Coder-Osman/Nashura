"""SimClashEnv: BattleEnvV1-shaped facade over SimEngine for P1.

Mirrors the host-side semantics that matter to agents: a decision every
DECISION_TICKS, 4-card hand with an 8-card cycle, elixir gating, placement
legality, and the PettingZoo parallel step() signature. Observations are plain
dicts for now; the P2 runner projects them onto the wire protocol.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import ruleset as R
from .engine import SimEngine
from .state import GameState


class SimClashEnvError(ValueError):
    pass


Action = Mapping[str, Any]  # {"kind": "play"|"wait", "hand_slot", "grid": [x, y]}

_DECK_26_HOG = (26000021, 26000038, 26000014, 26000010, 28000000, 28000011, 27000000, 26000030)


def default_deck() -> tuple[int, ...]:
    return _DECK_26_HOG


class SimClashEnv:
    def __init__(self, deck0: tuple[int, ...] | None = None, deck1: tuple[int, ...] | None = None,
                 seed: int = 1):
        self.deck0 = tuple(deck0 or _DECK_26_HOG)
        self.deck1 = tuple(deck1 or _DECK_26_HOG)
        self.seed = seed
        self.engine = SimEngine.create(self.deck0, self.deck1, seed)

    # ---------------------------------------------------------------- api --
    def reset(self, *, seed: int | None = None) -> dict[int, dict[str, Any]]:
        if seed is not None:
            self.seed = seed
        self.engine = SimEngine.create(self.deck0, self.deck1, self.seed)
        self.engine.step(R.FIRST_PLAYABLE_TICK)
        return self.observe()

    def step(self, commands: Mapping[int, tuple[Action, ...]]) -> tuple[
        dict[int, dict[str, Any]], dict[int, float], dict[int, bool], dict[int, bool], dict[str, Any]
    ]:
        """Apply one decision step for both owners, advance to the next deadline."""
        st = self.engine.state
        if st.ended:
            raise SimClashEnvError("match already ended")
        rejected: list[dict[str, Any]] = []
        for owner in (0, 1):
            for action in commands.get(owner, ()):
                if action.get("kind") == "wait":
                    continue
                ok = self._apply_action(owner, action)
                if not ok:
                    rejected.append({"owner": owner, **dict(action)})
        self.engine.step(R.DECISION_TICKS)
        observations = self.observe()
        terminated = {"__all__": st.ended, 0: st.ended, 1: st.ended}
        truncated = {"__all__": False, 0: False, 1: False}
        rewards = {owner: self._reward(owner) for owner in (0, 1)}
        infos = {"rejected_actions": rejected}
        return observations, rewards, terminated, truncated, infos

    def _apply_action(self, owner: int, action: Action) -> bool:
        hand_slot = action.get("hand_slot")
        grid = action.get("grid")
        if not isinstance(hand_slot, int) or not 0 <= hand_slot < 4:
            return False
        if not isinstance(grid, (tuple, list)) or len(grid) != 2:
            return False
        player = self.engine.state.players[owner]
        if hand_slot >= len(player.hand):
            return False
        card_id = player.hand[hand_slot]
        gx, gy = int(grid[0]), int(grid[1])
        x = int(min(R.WORLD_X_LIMIT - 1, max(0, gx * 1000 + 500)))
        y = int(min(R.WORLD_Y_LIMIT - 1, max(0, gy * 1000 + 500)))
        return self.engine.play_card(owner, card_id, x, y)

    # ----------------------------------------------------------- observation -
    def observe(self, owner: int | None = None) -> Any:
        st = self.engine.state
        if owner is None:
            return {owner: self._observe_owner(st, owner) for owner in (0, 1)}
        return self._observe_owner(st, owner)

    def _observe_owner(self, st: GameState, owner: int) -> dict[str, Any]:
        me = st.players[owner]
        foe = st.players[1 - owner]
        return {
            "tick": st.tick,
            "phase": "overtime" if st.tick >= R.REGULATION_END_TICK else "regulation",
            "elixir_exact": me.elixir_raw / 10_000.0,
            "hand": [{"card_id": c, "cost": R.CARDS[c].elixir_cost} for c in me.hand],
            "next_card": me.queue[0] if me.queue else None,
            "crowns": {"self": me.crowns, "opponent": foe.crowns},
            "towers": [self._tower_dict(t) for t in st.towers.values()],
            "entities": [self._entity_dict(e) for e in st.entities.values()],
            "projectiles": [
                {"id": p.projectile_id, "owner": p.owner, "x": int(p.x), "y": int(p.y), "is_roll": p.is_roll}
                for p in st.projectiles.values()
            ],
            "events": list(st.events),
            "terminal": {
                "ended": st.ended,
                "winner": st.winner,
                "reason": st.terminal_reason,
                "terminal_tick": st.terminal_tick,
            },
        }

    @staticmethod
    def _tower_dict(t: Any) -> dict[str, Any]:
        return {
            "entity_id": t.entity_id, "owner": t.owner, "kind": t.tower_kind,
            "x": t.x, "y": t.y, "hitpoints": max(0, t.hitpoints),
            "max_hitpoints": t.spec.hitpoints, "active": t.active,
        }

    @staticmethod
    def _entity_dict(e: Any) -> dict[str, Any]:
        return {
            "entity_id": e.entity_id, "owner": e.owner, "card_id": e.card.card_id,
            "name": e.card.name, "x": e.x, "y": e.y,
            "hitpoints": max(0, e.hitpoints), "max_hitpoints": e.card.hitpoints or 0,
            "deploying": e.deploying,
            "frozen": e.frozen(),
            "slowed": any(b.name == "IceGolemDeathSlow" for b in e.buffs),
        }

    def _reward(self, owner: int) -> float:
        """Zero-sum terminal reward; 0 while playing (matches evaluate usage)."""
        st = self.engine.state
        if not st.ended or st.winner is None:
            return 0.0
        return 1.0 if st.winner == owner else -1.0
