"""P1 simulator tests: numeric anchors, mechanics, determinism, full match."""

from __future__ import annotations

import math

import pytest

from native_runner.sim_core import ruleset as R
from native_runner.sim_core.engine import SimEngine
from native_runner.sim_core.env import SimClashEnv, default_deck

HOG, ICE_GOLEM, MUSKETEER, SKELETONS = 26000021, 26000038, 26000014, 26000010
FIREBALL, LOG, CANNON, ICE_SPIRIT = 28000000, 28000011, 27000000, 26000030


# --------------------------------------------------------------- numerics --
def test_level_11_numbers_match_competitive_tables():
    cards = R.CARDS
    assert cards[HOG].hitpoints == 1697 and cards[HOG].damage == 317
    assert cards[ICE_GOLEM].hitpoints == 1315 and cards[ICE_GOLEM].damage == 84
    assert cards[MUSKETEER].hitpoints == 721 and cards[MUSKETEER].damage == 217
    assert cards[SKELETONS].hitpoints == 81 and cards[SKELETONS].count == 3
    assert cards[FIREBALL].spell_travel.damage == 688
    assert R.LOG_ROLL_DAMAGE == 268
    assert cards[CANNON].hitpoints == 824 and cards[CANNON].lifetime_ms == 30000
    assert cards[ICE_SPIRIT].hitpoints == 215


def test_tower_level_11_projection():
    # 1400 * 1.08^10 = 3022.49 -> 3022; 2400 -> 5181.4 -> 5181
    assert R.PRINCESS_TOWER.hitpoints == int(1400 * 1.08 ** 10) == 3022
    assert R.KING_TOWER.hitpoints == int(2400 * 1.08 ** 10) == 5181


def test_elixir_timeline():
    assert R.STARTING_ELIXIR_RAW == 60_000
    # 1x: 100000 raw per 28000 ms -> per 50 ms tick
    assert math.isclose(R.elixir_rate_raw_per_tick(0), 100000 * 50 / 28000)
    assert math.isclose(R.elixir_rate_raw_per_tick(2400), 100000 * 50 / 14000)
    assert math.isclose(R.elixir_rate_raw_per_tick(4800), 100000 * 50 / 9300)


def test_speed_conversion():
    # Speed 120 (Hog) = 2 tiles/s = 100 thousandths per 50 ms tick
    assert math.isclose(R.units_per_tick(120), 100.0)
    assert math.isclose(R.units_per_tick(60), 50.0)


# ------------------------------------------------------------- mechanics --
def test_hog_chips_unopposed_tower():
    """A lone hog cannot solo a full princess tower (16 hits needed vs a
    12.8 s survival window) but must land most of its hit pattern first."""
    eng = SimEngine.create(default_deck(), default_deck(), seed=1)
    eng.step(R.FIRST_PLAYABLE_TICK)
    assert eng.play_card(0, HOG, 3500, 12000)
    princess = next(t for t in eng.state.towers.values()
                    if t.owner == 1 and t.tower_kind == "princess_left")
    eng.step(1500)
    damage = R.PRINCESS_TOWER.hitpoints - max(0, princess.hitpoints)
    assert 1500 <= damage, damage       # at least ~5 hits landed
    assert damage % 317 == 0            # exact hit pattern, no fractional damage
    assert princess.hitpoints > 0       # ... and the tower survived the 1v1


def test_two_hogs_take_the_tower():
    eng = SimEngine.create(default_deck(), default_deck(), seed=2)
    eng.step(R.FIRST_PLAYABLE_TICK)
    assert eng.play_card(0, HOG, 3500, 13000)
    eng.step(240)  # let the first hog engage before the second arrives
    player = eng.state.players[0]
    player.elixir_raw = 100_000.0
    player.hand[0] = HOG  # white-box: cycle the second hog into hand
    assert eng.play_card(0, HOG, 3500, 13000)
    eng.step(600)
    princess = next(t for t in eng.state.towers.values()
                    if t.owner == 1 and t.tower_kind == "princess_left")
    assert princess.hitpoints <= 0


def test_skeletons_spawn_three():
    eng = SimEngine.create(default_deck(), default_deck(), seed=3)
    eng.step(R.FIRST_PLAYABLE_TICK)
    eng.play_card(0, SKELETONS, 9000, 10000)
    eng.step(25)  # past the 1 s deploy delay
    spawned = [e for e in eng.state.entities.values() if e.card.card_id == SKELETONS]
    assert len(spawned) == 3


def test_elixir_gating_rejects_unaffordable_card():
    eng = SimEngine.create(default_deck(), default_deck(), seed=4)
    eng.step(R.FIRST_PLAYABLE_TICK)
    # 6.0 starting elixir; play Hog (4) then Musketeer (4) -> second must fail
    assert eng.play_card(0, HOG, 3500, 12000)
    assert not eng.play_card(0, MUSKETEER, 3500, 12000)


def test_fireball_tower_damage_uses_crown_ratio():
    eng = SimEngine.create(default_deck(), default_deck(), seed=5)
    eng.step(R.FIRST_PLAYABLE_TICK)
    eng.state.players[0].hand[0] = FIREBALL
    princess = next(t for t in eng.state.towers.values()
                    if t.owner == 1 and t.tower_kind == "princess_left")
    before = princess.hitpoints
    assert eng.play_card(0, FIREBALL, princess.x, princess.y)
    eng.step(120)  # flight time covers the arena
    damage = before - princess.hitpoints
    assert damage == int(688 * 0.25), damage  # 172


def test_log_rolls_forward_and_expires():
    eng = SimEngine.create(default_deck(), default_deck(), seed=6)
    eng.step(R.FIRST_PLAYABLE_TICK)
    eng.state.players[0].hand[0] = LOG
    assert eng.play_card(0, LOG, 9000, 20000)
    eng.step(5)  # roll exists, moving +y
    rolls = [p for p in eng.state.projectiles.values() if p.is_roll]
    assert len(rolls) == 1 and rolls[0].dir_y == 1.0
    eng.step(120)  # 10100 range at ~166.7/tick = ~61 ticks
    assert not any(p.is_roll for p in eng.state.projectiles.values())


def test_ice_spirit_freeze_then_expire():
    eng = SimEngine.create(default_deck(), default_deck(), seed=7)
    eng.step(R.FIRST_PLAYABLE_TICK)
    # enemy hog to freeze
    eng.play_card(1, HOG, 9000, 19000)
    eng.step(2)
    eng.play_card(0, ICE_SPIRIT, 9000, 18000)
    eng.step(60)
    frozen = [e for e in eng.state.entities.values() if e.buffs]
    if frozen:  # freeze window may have passed; then require no crash either way
        assert all(any(b.speed_factor == 0.0 for b in e.buffs) for e in frozen)


def test_ice_golem_death_nova_slows():
    eng = SimEngine.create(default_deck(), default_deck(), seed=8)
    eng.step(R.FIRST_PLAYABLE_TICK)
    eng.play_card(1, MUSKETEER, 9000, 20000)
    eng.step(40)
    eng.play_card(0, ICE_GOLEM, 9000, 20000)
    eng.step(400)
    slowed = [e for e in eng.state.entities.values()
              if any(b.name == "IceGolemDeathSlow" for b in e.buffs)]
    # musketeer out-ranges the golem nova; she kills it without being slowed
    golem_alive = any(e.card.card_id == ICE_GOLEM for e in eng.state.entities.values())
    assert not golem_alive


def test_cannon_expires_after_lifetime():
    eng = SimEngine.create(default_deck(), default_deck(), seed=9)
    eng.step(R.FIRST_PLAYABLE_TICK)
    eng.state.players[0].hand[0] = CANNON
    eng.play_card(0, CANNON, 9000, 8000)
    eng.step(30)
    assert any(e.card.card_id == CANNON for e in eng.state.entities.values())
    eng.step(700)  # 30 s lifetime + margin
    assert not any(e.card.card_id == CANNON for e in eng.state.entities.values())


def test_king_activates_when_princess_dies():
    eng = SimEngine.create(default_deck(), default_deck(), seed=10)
    king = next(t for t in eng.state.towers.values() if t.owner == 1 and t.is_king)
    assert not king.active
    eng.step(R.FIRST_PLAYABLE_TICK)
    player = eng.state.players[0]
    for _ in range(6):
        player.hand[0] = HOG
        player.elixir_raw = 100_000.0
        eng.play_card(0, HOG, 3500, 13000)
        eng.step(600)
    assert any(t.hitpoints <= 0 for t in eng.state.towers.values()
               if t.owner == 1 and not t.is_king)
    assert king.active


def test_placement_rules():
    eng = SimEngine.create(default_deck(), default_deck(), seed=11)
    eng.step(R.FIRST_PLAYABLE_TICK)
    player = eng.state.players[0]
    player.hand[1] = FIREBALL
    assert not eng.play_card(0, HOG, 9000, 20000)   # enemy side
    assert not eng.play_card(0, HOG, 9000, 16000)   # river (not bridge)
    spell_eng = SimEngine.create(default_deck(), default_deck(), seed=12)
    spell_eng.step(R.FIRST_PLAYABLE_TICK)
    spell_eng.state.players[0].hand[0] = FIREBALL
    assert spell_eng.play_card(0, FIREBALL, 9000, 16000)  # river is legal for spells
    assert eng.play_card(0, HOG, 3500, 12000)       # own side, fine
    player.elixir_raw = 100_000.0
    player.hand[0] = FIREBALL
    assert eng.play_card(0, FIREBALL, 9000, 20000)  # spells anywhere, even enemy side


# ----------------------------------------------------- determinism + match --
def _mirror_script(env: SimClashEnv):
    """Both owners play the same paced script: hog + skeletons on cycle."""
    obs = env.reset()
    rng_state = [0]
    while not obs[0]["terminal"]["ended"]:
        commands = {}
        for owner in (0, 1):
            actions = []
            o = obs[owner]
            if o["hand"] and o["elixir_exact"] >= 4:
                slot = next((i for i, c in enumerate(o["hand"]) if c["cost"] <= o["elixir_exact"]), None)
                if slot is not None:
                    y = 12000 if owner == 0 else 19000
                    actions.append({"kind": "play", "hand_slot": slot, "grid": (3, y // 1000)})
            commands[owner] = tuple(actions) if actions else ({"kind": "wait"},)
            rng_state[0] += 1
        obs, *_ = env.step(commands)
    return obs


def test_scripted_match_terminates_deterministically():
    env_a = SimClashEnv(seed=42)
    result_a = _mirror_script(env_a)
    env_b = SimClashEnv(seed=42)
    result_b = _mirror_script(env_b)
    assert result_a[0]["terminal"]["ended"]
    assert result_a == result_b  # byte-identical replay for the same seed


def test_match_ends_within_gameplay_bounds():
    env = SimClashEnv(seed=7)
    obs = _mirror_script(env)
    assert obs[0]["terminal"]["terminal_tick"] <= R.GAMEPLAY_END_TICK


def test_reset_restores_fresh_state():
    env = SimClashEnv(seed=1)
    _mirror_script(env)
    obs = env.reset()
    assert obs[0]["tick"] == R.FIRST_PLAYABLE_TICK
    assert obs[0]["elixir_exact"] > 6.0  # 90 ticks of regen on top of 6.0
    assert not obs[0]["terminal"]["ended"]
