"""Deterministic tick loop for the P1 simulator.

Tick order is fixed: scheduled deployments -> elixir -> buffs -> entity
behaviour -> towers -> projectiles -> deaths/terminal. No RNG anywhere, so the
same seed plus the same action stream always replays identically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import ruleset as R
from .state import Buff, Entity, GameState, PlayerState, Projectile, Tower

_EPS = 1e-9
_ARRIVAL_RADIUS = 250  # homing projectiles detonate within this distance


@dataclass
class Deployment:
    """A card play accepted by the env, materialised after the deploy delay."""
    due_tick: int
    owner: int
    card_id: int
    x: int
    y: int


@dataclass
class SimEngine:
    state: GameState
    deployments: list[Deployment] = field(default_factory=list)

    # ------------------------------------------------------------- setup ----
    @classmethod
    def create(cls, deck0: Iterable[int], deck1: Iterable[int], seed: int) -> "SimEngine":
        st = GameState(seed=seed)
        for owner, deck in ((0, deck0), (1, deck1)):
            cards = tuple(deck)
            if len(cards) != 8:
                raise ValueError(f"deck{owner} must have 8 cards")
            unknown = [c for c in cards if c not in R.CARDS]
            if unknown:
                raise ValueError(f"deck{owner} cards outside P1 subset: {unknown}")
            player = PlayerState(owner=owner, deck=cards)
            player.hand = list(cards[:4])
            player.queue = list(cards[4:])
            st.players[owner] = player
        for (owner, kind), (x, y) in R.TOWER_POSITIONS.items():
            spec = R.KING_TOWER if kind == "king" else R.PRINCESS_TOWER
            entity_id = 1000 + owner * 10 + (0 if kind == "king" else (1 if kind == "princess_left" else 2))
            st.towers[entity_id] = Tower(
                entity_id=entity_id, owner=owner, tower_kind=kind, spec=spec,
                x=x, y=y, hitpoints=spec.hitpoints, active=(kind != "king"),
            )
        return cls(state=st)

    # ------------------------------------------------------------- playing --
    def play_card(self, owner: int, card_id: int, x: int, y: int) -> bool:
        """Queue a card play at the current tick; returns False when illegal."""
        player = self.state.players[owner]
        if card_id not in player.hand:
            return False
        if self.state.tick < R.FIRST_PLAYABLE_TICK:
            return False
        cost = R.CARDS[card_id].elixir_cost * 10_000
        if player.elixir_raw + _EPS < cost:
            return False
        spec = R.CARDS[card_id]
        if spec.kind != "spell" and not self._placement_legal(owner, spec, x, y):
            return False
        player.elixir_raw -= cost
        slot = player.hand.index(card_id)
        player.queue.append(card_id)
        player.hand[slot] = player.queue.pop(0)  # next card cycles into the slot
        # Commands sent on tick T apply on T+1 (boundary = applied - 1,
        # wire-spec section 4.3); units then need their deploy delay.
        deploy_delay = 0 if spec.kind == "spell" else spec.deploy_time_ms // R.TICK_MS
        self.deployments.append(
            Deployment(due_tick=self.state.tick + 1 + deploy_delay, owner=owner,
                       card_id=card_id, x=x, y=y)
        )
        self.state.log_event("card_play", owner=owner, card_id=card_id, x=x, y=y,
                             due_tick=self.tick_when_ready(card_id))
        return True

    def tick_when_ready(self, card_id: int) -> int:
        spec = R.CARDS[card_id]
        if spec.kind == "spell":
            return self.state.tick + 1
        return self.state.tick + 1 + spec.deploy_time_ms // R.TICK_MS

    def _placement_legal(self, owner: int, spec: R.CardSpec, x: int, y: int) -> bool:
        if not (0 <= x < R.WORLD_X_LIMIT and 0 <= y < R.WORLD_Y_LIMIT):
            return False
        if self._in_river(x, y) and spec.kind != "spell":
            return False
        if spec.kind == "building":
            if owner == 0 and y >= R.RIVER_Y_MIN:
                return False
            if owner == 1 and y <= R.RIVER_Y_MAX:
                return False
            for t in self.state.towers.values():
                if t.hitpoints > 0 and self._dist(x, y, t.x, t.y) < t.spec.collision_radius + spec.collision_radius:
                    return False
            for e in self.state.entities.values():
                if e.is_building and e.hitpoints > 0 and self._dist(x, y, e.x, e.y) < e.card.collision_radius * 2:
                    return False
            return True
        # Troops: own half only. A destroyed enemy princess tower opens its
        # lane on the opponent side (battle_env destroyed_enemy_princess_lanes).
        own_side = y < R.RIVER_Y_MIN if owner == 0 else y > R.RIVER_Y_MAX
        if own_side:
            return True
        for tower_id, tower in self.state.towers.items():
            if tower.owner != owner and not tower.is_king and tower.hitpoints <= 0:
                lane_x = tower.x
                if abs(x - lane_x) <= 3000:
                    if owner == 0 and y < R.WORLD_Y_LIMIT - 2000:
                        return True
                    if owner == 1 and y > 2000:
                        return True
        return False

    @staticmethod
    def _in_river(x: int, y: int) -> bool:
        if not (R.RIVER_Y_MIN < y < R.RIVER_Y_MAX):
            return False
        return not any(abs(x - bx) <= R.BRIDGE_HALF_WIDTH for bx in R.BRIDGE_X)

    @staticmethod
    def _dist(ax: float, ay: float, bx: float, by: float) -> float:
        return math.hypot(ax - bx, ay - by)

    # -------------------------------------------------------------- stepping -
    def step(self, ticks: int) -> None:
        for _ in range(ticks):
            if self.state.ended:
                return
            self._tick()

    def _tick(self) -> None:
        st = self.state
        st.tick += 1
        self._materialise_deployments()
        self._elixir()
        self._decay_buffs()
        for entity in sorted(st.entities.values(), key=lambda e: e.entity_id):
            if entity.hitpoints > 0:
                self._entity_behaviour(entity)
        for tower in sorted(st.towers.values(), key=lambda t: t.entity_id):
            if tower.hitpoints > 0 and tower.active:
                self._tower_behaviour(tower)
        for proj in sorted(st.projectiles.values(), key=lambda p: p.projectile_id):
            self._advance_projectile(proj)
        self._building_lifetimes()
        self._check_terminal()

    # ------------------------------------------------------------ substeps --
    def _materialise_deployments(self) -> None:
        st = self.state
        due = [d for d in self.deployments if d.due_tick <= st.tick]
        self.deployments = [d for d in self.deployments if d.due_tick > st.tick]
        for d in due:
            spec = R.CARDS[d.card_id]
            if spec.kind == "spell":
                self._cast_spell(d.owner, spec, d.x, d.y)
            else:
                for offset_index, (ox, oy) in enumerate(spec.formation[: spec.count]):
                    if spec.count <= 1:
                        ox = oy = 0
                    eid = st.alloc_entity_id()
                    st.entities[eid] = Entity(
                        entity_id=eid, owner=d.owner, card=spec,
                        x=int(max(0, min(R.WORLD_X_LIMIT - 1, d.x + ox))),
                        y=int(max(0, min(R.WORLD_Y_LIMIT - 1, d.y + oy))),
                        hitpoints=spec.hitpoints or 0,
                        deploy_remaining_ms=0,  # deploy delay consumed pre-spawn
                        lifetime_remaining_ms=spec.lifetime_ms,
                        windup_remaining_ms=spec.load_time_ms or None,
                    )
                st.log_event("spawn", owner=d.owner, card_id=d.card_id, x=d.x, y=d.y)

    def _cast_spell(self, owner: int, spec: R.CardSpec, x: int, y: int) -> None:
        st = self.state
        if spec.card_id == 28000011:  # The Log rolls from the cast point
            pid = st.alloc_entity_id()
            st.projectiles[pid] = Projectile(
                projectile_id=pid, owner=owner, x=float(x), y=float(y),
                is_roll=True, dir_y=1.0 if owner == 0 else -1.0,
                speed_per_tick=R.units_per_tick(R.LOG_ROLL_SPEED),
                remaining_range=float(spec.roll_range or 0), width=spec.roll_width or 0,
                damage=R.LOG_ROLL_DAMAGE,
            )
            st.log_event("spell_cast", owner=owner, card_id=spec.card_id, x=x, y=y)
            return
        travel = spec.spell_travel
        if travel is None:
            return
        origin = next(t for t in st.towers.values() if t.owner == owner and t.is_king)
        pid = st.alloc_entity_id()
        st.projectiles[pid] = Projectile(
            projectile_id=pid, owner=owner,
            x=float(origin.x), y=float(origin.y),
            dest_x=float(x), dest_y=float(y),
            speed_per_tick=R.units_per_tick(travel.speed), damage=travel.damage,
            spec=travel,
        )
        st.log_event("spell_cast", owner=owner, card_id=spec.card_id, x=x, y=y)

    def _elixir(self) -> None:
        rate = R.elixir_rate_raw_per_tick(self.state.tick)
        for player in self.state.players.values():
            player.elixir_raw = min(float(R.ELIXIR_CAP_RAW), player.elixir_raw + rate)

    def _decay_buffs(self) -> None:
        for holder in list(self.state.entities.values()) + list(self.state.towers.values()):
            for buff in holder.buffs:
                buff.remaining_ms -= R.TICK_MS
            holder.buffs = [b for b in holder.buffs if b.remaining_ms > 0]

    # -------------------------------------------------------------- combat --
    def _entity_behaviour(self, entity: Entity) -> None:
        if entity.deploying:
            entity.deploy_remaining_ms -= R.TICK_MS
            return
        if entity.frozen():
            return
        target = self._current_target(entity)
        if target is None:
            return
        dist = self._attack_distance(entity, entity.card, target)
        if dist <= entity.card.range_thousandths:
            self._attack_tick(entity, entity.card, target)
        else:
            self._move_toward(entity, target)

    def _current_target(self, obj: Entity | Tower) -> Entity | Tower | None:
        """Targeting: keep current target while valid, else nearest eligible."""
        st = self.state
        current: Entity | Tower | None = None
        if isinstance(obj, Entity) and obj.target_entity_id is not None:
            current = st.entities.get(obj.target_entity_id)
            if current is None or current.hitpoints <= 0:
                current = st.towers.get(obj.target_entity_id)
        if isinstance(obj, Tower) and obj.target_entity_id is not None:
            current = st.entities.get(obj.target_entity_id)
        if current is not None and current.hitpoints > 0 and self._targetable(obj, current):
            return current
        candidates: list[tuple[float, int, Entity | Tower]] = []
        if isinstance(obj, Entity):
            spec = obj.card
            sight = spec.sight_thousandths
            want_buildings_only = spec.targets_only_buildings
        else:
            sight = obj.spec.sight_thousandths
            want_buildings_only = False
        if not want_buildings_only:
            for e in st.entities.values():
                if e.owner == obj.owner or e.hitpoints <= 0:
                    continue
                if self._targetable(obj, e):
                    d = self._dist(obj.x, obj.y, e.x, e.y)
                    if d <= sight:
                        candidates.append((d, e.entity_id, e))
        for t in st.towers.values():
            if t.owner == obj.owner or t.hitpoints <= 0:
                continue
            if self._targetable(obj, t):
                d = self._dist(obj.x, obj.y, t.x, t.y)
                if d <= sight or want_buildings_only:
                    candidates.append((d, t.entity_id, t))
        if not candidates:
            if isinstance(obj, Entity):
                return self._default_tower_target(obj)
            return None
        candidates.sort(key=lambda c: (c[0], c[1]))
        chosen = candidates[0][2]
        if isinstance(obj, Entity):
            obj.target_entity_id = chosen.entity_id
            if obj.windup_remaining_ms is None and obj.card.load_time_ms:
                obj.windup_remaining_ms = obj.card.load_time_ms
        else:
            obj.target_entity_id = chosen.entity_id
            if obj.windup_remaining_ms is None and obj.spec.load_time_ms:
                obj.windup_remaining_ms = obj.spec.load_time_ms
        return chosen

    def _default_tower_target(self, entity: Entity) -> Tower | None:
        """No enemy in sight: push the x-nearest enemy princess tower, else king."""
        enemy_towers = [t for t in self.state.towers.values()
                        if t.owner != entity.owner and t.hitpoints > 0]
        if not enemy_towers:
            return None
        princesses = [t for t in enemy_towers if not t.is_king]
        pool = princesses or [t for t in enemy_towers if t.is_king]
        pool.sort(key=lambda t: (self._dist(entity.x, entity.y, t.x, t.y), t.entity_id))
        chosen = pool[0]
        entity.target_entity_id = chosen.entity_id
        return chosen

    def _targetable(self, attacker: Entity | Tower, target: Entity | Tower) -> bool:
        if isinstance(target, Tower):
            return True
        if isinstance(attacker, Entity):
            if target.airborne and not attacker.card.attacks_air:
                return False
            return True
        return True  # towers attack air and ground

    def _attack_distance(self, obj: Entity | Tower, spec_like: Any, target: Entity | Tower) -> float:
        radius = target.spec.collision_radius if isinstance(target, Tower) else target.card.collision_radius
        return self._dist(obj.x, obj.y, target.x, target.y) - radius

    def _attack_tick(self, obj: Entity | Tower, spec_like: Any, target: Entity | Tower) -> None:
        interval = obj.attack_interval_ms() if isinstance(obj, Entity) else obj.spec.hit_speed_ms
        if obj.windup_remaining_ms is not None:
            obj.windup_remaining_ms -= R.TICK_MS
            if obj.windup_remaining_ms > 0:
                return
            obj.windup_remaining_ms = None
        if obj.attack_cooldown_ms > 0:
            obj.attack_cooldown_ms -= R.TICK_MS
            return
        obj.attack_cooldown_ms = interval
        if isinstance(obj, Entity) and obj.card.kamikaze and obj.card.projectile is not None:
            # Ice Spirit launches its payload then dies.
            self._spawn_projectile(obj, target)
            self._kill_entity(obj, "kamikaze")
            return
        if isinstance(obj, Entity) and obj.card.projectile is not None:
            self._spawn_projectile(obj, target)
            return
        damage = obj.card.damage if isinstance(obj, Entity) else obj.spec.damage
        self._apply_damage(attacker_owner=obj.owner, target=target, amount=damage,
                           tower_ratio=1.0)
        if isinstance(obj, Entity) and obj.card.kamikaze:
            self._kill_entity(obj, "kamikaze")

    def _spawn_projectile(self, entity: Entity, target: Entity | Tower) -> None:
        spec = entity.card.projectile
        if spec is None:
            return
        st = self.state
        pid = st.alloc_entity_id()
        st.projectiles[pid] = Projectile(
            projectile_id=pid, owner=entity.owner,
            x=float(entity.x), y=float(entity.y),
            target_entity_id=target.entity_id if isinstance(target, Entity) else None,
            target_tower_id=target.entity_id if isinstance(target, Tower) else None,
            speed_per_tick=R.units_per_tick(spec.speed), damage=spec.damage, spec=spec,
        )

    def _move_toward(self, entity: Entity, target: Entity | Tower) -> None:
        speed = entity.effective_speed_units_per_tick()
        if speed <= 0:
            return
    def _move_toward(self, entity: Entity, target: Entity | Tower) -> None:
        speed = entity.effective_speed_units_per_tick()
        if speed <= 0:
            return
        tx, ty = float(target.x), float(target.y)
        if self._needs_bridge(entity, target):
            bx = min(R.BRIDGE_X, key=lambda b: abs(entity.x - b) + abs(target.x - b))
            exit_y = R.RIVER_Y_MAX + 600 if target.y > entity.y else R.RIVER_Y_MIN - 600
            tx, ty = float(bx), float(exit_y)
        dx, dy = tx - entity.x, ty - entity.y
        norm = math.hypot(dx, dy)
        if norm < _EPS:
            return
        entity.x = int(round(entity.x + dx / norm * speed))
        entity.y = int(round(entity.y + dy / norm * speed))
        entity.x = max(0, min(R.WORLD_X_LIMIT - 1, entity.x))
        entity.y = max(0, min(R.WORLD_Y_LIMIT - 1, entity.y))

    def _needs_bridge(self, entity: Entity, target: Entity | Tower) -> bool:
        own_above = entity.y > R.RIVER_Y_MAX
        target_above = target.y > R.RIVER_Y_MAX
        own_below = entity.y < R.RIVER_Y_MIN
        target_below = target.y < R.RIVER_Y_MIN
        crossed = (own_above and target_below) or (own_below and target_above)
        in_band = R.RIVER_Y_MIN <= entity.y <= R.RIVER_Y_MAX
        return crossed or in_band

    # ------------------------------------------------------------- towers ---
    def _tower_behaviour(self, tower: Tower) -> None:
        if tower.frozen():
            return
        target = self._current_target(tower)
        if target is None:
            return
        dist = self._attack_distance(tower, tower.spec, target)
        if dist > tower.spec.range_thousandths:
            return  # towers do not move
        self._attack_tick(tower, tower.spec, target)

    # --------------------------------------------------------- projectiles --
    def _advance_projectile(self, proj: Projectile) -> None:
        st = self.state
        if proj.is_roll:
            self._advance_roll(proj)
            return
        tx = ty = None
        target: Entity | Tower | None = None
        if proj.target_entity_id is not None:
            target = st.entities.get(proj.target_entity_id)
            if target is not None and target.hitpoints > 0:
                tx, ty = float(target.x), float(target.y)
        if target is None and proj.target_tower_id is not None:
            tower = st.towers.get(proj.target_tower_id)
            if tower is not None and tower.hitpoints > 0:
                target = tower
                tx, ty = float(tower.x), float(tower.y)
        if tx is None:
            tx, ty = proj.dest_x, proj.dest_y
        if tx is None or ty is None:
            del st.projectiles[proj.projectile_id]
            return
        dx, dy = tx - proj.x, ty - proj.y
        norm = math.hypot(dx, dy)
        step = proj.speed_per_tick
        if norm <= max(step, _ARRIVAL_RADIUS):
            if proj.spec is not None and proj.spec.aoe_radius:
                self._aoe_impact(proj, proj.x, proj.y)
            elif target is not None:
                self._apply_damage(proj.owner, target, proj.damage, self._tower_ratio(proj))
                self._projectile_side_effects(proj, target)
            st.log_event("projectile_impact", owner=proj.owner, x=proj.x, y=proj.y,
                         damage=proj.damage)
            st.projectiles.pop(proj.projectile_id, None)
            return
        proj.x += dx / norm * step
        proj.y += dy / norm * step

    def _advance_roll(self, proj: Projectile) -> None:
        st = self.state
        step = min(proj.speed_per_tick, proj.remaining_range)
        old_x, old_y = proj.x, proj.y
        proj.x += proj.dir_x * step
        proj.y += proj.dir_y * step
        proj.remaining_range -= step
        # swept rectangle: enemies between old and new position within width
        for enemy in sorted(st.entities.values(), key=lambda e: e.entity_id):
            if enemy.owner == proj.owner or enemy.hitpoints <= 0 or enemy.entity_id in proj.hit_entity_ids:
                continue
            if enemy.airborne:
                continue
            lateral = abs(enemy.x - proj.x)
            along_old = (enemy.y - old_y) * proj.dir_y
            along_new = (enemy.y - proj.y) * proj.dir_y
            if lateral <= proj.width and (along_old <= 0 <= along_new or abs(enemy.y - proj.y) <= step):
                proj.hit_entity_ids.add(enemy.entity_id)
                self._apply_damage(proj.owner, enemy, proj.damage, R.LOG_TOWER_RATIO)
        for tower_id, tower in sorted(st.towers.items()):
            if tower.owner == proj.owner or tower.hitpoints <= 0 or tower_id in proj.hit_tower_ids:
                continue
            lateral = abs(tower.x - proj.x)
            along_new = (tower.y - proj.y) * proj.dir_y
            if lateral <= proj.width + tower.spec.collision_radius and -step <= along_new <= step * 2:
                proj.hit_tower_ids.add(tower_id)
                self._apply_damage(proj.owner, tower, proj.damage, R.LOG_TOWER_RATIO)
        if proj.remaining_range <= _EPS or proj.y <= 0 or proj.y >= R.WORLD_Y_LIMIT:
            st.projectiles.pop(proj.projectile_id, None)

    def _aoe_impact(self, proj: Projectile, cx: float, cy: float) -> None:
        st = self.state
        radius = proj.spec.aoe_radius if proj.spec else 0
        for enemy in sorted(st.entities.values(), key=lambda e: e.entity_id):
            if enemy.owner == proj.owner or enemy.hitpoints <= 0:
                continue
            if self._dist(cx, cy, enemy.x, enemy.y) <= radius + enemy.card.collision_radius:
                self._apply_damage(proj.owner, enemy, proj.damage, self._tower_ratio(proj))
                self._projectile_side_effects(proj, enemy)
        for tower_id, tower in sorted(st.towers.items()):
            if tower.owner == proj.owner or tower.hitpoints <= 0:
                continue
            if self._dist(cx, cy, tower.x, tower.y) <= radius + tower.spec.collision_radius:
                self._apply_damage(proj.owner, tower, proj.damage, self._tower_ratio(proj))

    def _tower_ratio(self, proj: Projectile) -> float:
        if proj.spec is not None:
            return proj.spec.crown_tower_ratio
        return R.LOG_TOWER_RATIO if proj.is_roll else 1.0

    def _projectile_side_effects(self, proj: Projectile, target: Entity | Tower) -> None:
        if proj.spec is None:
            return
        if proj.spec.freeze_ms and isinstance(target, Entity):
            target.buffs.append(Buff("Freeze", proj.spec.freeze_ms, 0.0, 0.0))

    # ------------------------------------------------------------ damage ----
    def _apply_damage(self, attacker_owner: int, target: Entity | Tower, amount: int, tower_ratio: float) -> None:
        st = self.state
        if isinstance(target, Tower):
            damage = int(amount * tower_ratio)
            if damage <= 0:
                return
            target.hitpoints -= damage
            if target.is_king and not target.active:
                target.active = True  # king activates on first damage
            st.log_event("tower_damage", owner=attacker_owner, tower_id=target.entity_id,
                         amount=damage, remaining_hp=max(0, target.hitpoints))
            if target.hitpoints <= 0:
                self._tower_destroyed(target)
        else:
            target.hitpoints -= amount
            st.log_event("damage", owner=attacker_owner, target=target.entity_id,
                         amount=amount, remaining_hp=max(0, target.hitpoints))
            if target.hitpoints <= 0:
                self._kill_entity(target, "killed")

    def _kill_entity(self, entity: Entity, reason: str) -> None:
        st = self.state
        entity.hitpoints = 0
        st.log_event("death", owner=entity.owner, entity_id=entity.entity_id, reason=reason)
        card = entity.card
        if card.death_aoe_radius and card.death_aoe_damage:
            for enemy in sorted(st.entities.values(), key=lambda e: e.entity_id):
                if enemy.owner == entity.owner or enemy.hitpoints <= 0:
                    continue
                if self._dist(entity.x, entity.y, enemy.x, enemy.y) <= card.death_aoe_radius + enemy.card.collision_radius:
                    self._apply_damage(entity.owner, enemy, card.death_aoe_damage, 1.0)
                    enemy.buffs.append(Buff("IceGolemDeathSlow", card.death_aoe_slow_ms,
                                            card.death_aoe_slow_factor, card.death_aoe_slow_factor))
            for tower_id, tower in sorted(st.towers.items()):
                if tower.owner == entity.owner or tower.hitpoints <= 0:
                    continue
                if self._dist(entity.x, entity.y, tower.x, tower.y) <= card.death_aoe_radius + tower.spec.collision_radius:
                    self._apply_damage(entity.owner, tower, card.death_aoe_damage, 1.0)
        st.entities.pop(entity.entity_id, None)

    def _tower_destroyed(self, tower: Tower) -> None:
        st = self.state
        destroyer = 1 - tower.owner
        st.log_event("tower_destroyed", owner=destroyer, tower_id=tower.entity_id,
                     tower_kind=tower.tower_kind)
        if tower.is_king:
            st.players[destroyer].crowns = 3
            self._finish(destroyer, "king_tower_destroyed")
            return
        st.players[destroyer].crowns += 1
        # destroying a princess tower activates the friendly king
        for friendly in st.towers.values():
            if friendly.owner == tower.owner and friendly.is_king and friendly.hitpoints > 0:
                friendly.active = True
        if st.tick >= R.REGULATION_END_TICK:
            # Overtime sudden death: the first tower to fall ends the match.
            self._finish(destroyer, "sudden_death")

    def _building_lifetimes(self) -> None:
        for entity in sorted(self.state.entities.values(), key=lambda e: e.entity_id):
            if entity.is_building and entity.hitpoints > 0 and entity.lifetime_remaining_ms is not None:
                entity.lifetime_remaining_ms -= R.TICK_MS
                if entity.lifetime_remaining_ms <= 0:
                    self.state.log_event("expire", owner=entity.owner,
                                         entity_id=entity.entity_id)
                    self.state.entities.pop(entity.entity_id, None)

    # ------------------------------------------------------------ terminal --
    def _check_terminal(self) -> None:
        st = self.state
        if st.ended:
            return
        if st.tick >= R.GAMEPLAY_END_TICK:
            crowns0, crowns1 = st.players[0].crowns, st.players[1].crowns
            if crowns0 > crowns1:
                self._finish(0, "crown_advantage")
            elif crowns1 > crowns0:
                self._finish(1, "crown_advantage")
            else:
                self._finish(None, "draw")

    def _finish(self, winner: int | None, reason: str) -> None:
        st = self.state
        st.ended = True
        st.winner = winner
        st.terminal_reason = reason
        st.terminal_tick = st.tick
        st.log_event("terminal", winner=winner, reason=reason, tick=st.tick)
