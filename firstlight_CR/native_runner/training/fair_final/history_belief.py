from itertools import permutations
import random
import time

import numpy as np
import torch

from native_runner.contracts import ActionV1
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.event_action_space import event_cell_to_native
from ..fair_search.belief import NativeConditioner, OwnInitialDraw, initial_draw
from ..fair_search.config import HOG_DECK
from ..fair_search.contracts import semantic_digest
from .history import FairHistory, PublicDeployment
from .native import FinalBattleEnv
from .policy import FullAction
from .rollout import FinalRollout
from .search import split_seed
from .visibility import event_visible_to_owner, validate_fair_view


class IncompatibleHistory(ValueError):
    pass


def compatible_opponent_draw(history, seed):
    draws = np.asarray(list(permutations(HOG_DECK)), dtype=np.int64)
    initial = draws.copy()
    for event in history.deployments:
        if event.owner == history.opening.owner:
            continue
        keep = (draws[:, :4] == event.card_id).any(axis=1)
        draws, initial = draws[keep], initial[keep]
        if not len(draws):
            raise IncompatibleHistory("no fixed-deck permutation matches public card cycles")
        slots = (draws[:, :4] == event.card_id).argmax(axis=1)
        draws[np.arange(len(draws)), slots] = draws[:, 4]
        draws[:, 4:7] = draws[:, 5:8]
        draws[:, 7] = event.card_id
    return tuple(map(int, initial[random.Random(seed).randrange(len(initial))]))


def deployment_action(rollout, information, event):
    duration = event.tick - information.tick
    if duration < 2 or duration > 60 or duration % 2 or event.owner != information.owner:
        return None
    environment = rollout.runtime.environment
    observation = rollout.runtime.frozen[information.owner]
    validate_fair_view(observation, information.owner)
    own = next(player for player in observation.players if player.owner == information.owner)
    slots = own.metadata["hand_slot_by_card"]
    slot = slots.get(str(event.card_id), slots.get(event.card_id))
    if slot is None:
        return None
    placement = environment.action_mask(information.owner).placement_masks.get(str(slot))
    if placement is None:
        return None
    offset = placement.get("model_subcell_offset")
    if offset is not None:
        sign = 1 if information.owner == 0 else -1
        offset = tuple(float(value) * sign for value in offset)
    matches = []
    if event.card_id not in information.legality.card_ids:
        return None
    card_index = information.legality.card_ids.index(event.card_id)
    for cell in torch.where(information.legality.plays[card_index, :, duration // 2])[0].tolist():
        grid = event_cell_to_native(cell, owner=information.owner, board_width=18)
        command = ActionV1.play(information.owner, int(slot), grid, subcell_offset=offset, card_id=event.card_id)
        if tuple(environment._world_target(command)) == (event.x, event.y):
            matches.append(FullAction(event.card_id, cell, duration // 2))
    if len(matches) > 1:
        raise ValueError("public deployment maps to ambiguous model cells")
    return matches[0] if matches else None


class HistoryReplaySession:
    def __init__(self, rollout, *, opening, opponent, seed, sidecar_factory, maximum_plan_attempts=128):
        if type(maximum_plan_attempts) is not int or maximum_plan_attempts < 1:
            raise ValueError("hidden plan sampling must have a finite positive budget")
        information = rollout.freeze_due_information()[opening.owner]
        if information.sha256 != opening.root_fair_sha:
            raise IncompatibleHistory("independent opening differs from own FAIR evidence")
        self.rollout, self.opponent, self.seed = rollout, opponent, seed
        self.owner = opening.owner
        self.maximum_plan_attempts = maximum_plan_attempts
        self.history = FairHistory(opening, (), (), opening.tick, opening.root_fair_sha)
        self.root = rollout.snapshot(sidecar_factory)
        self.sidecar_factory = sidecar_factory
        self.closed = False
        self.profile = dict(syncs=0, ticks=0, cold_ticks=0, incremental_ticks=0, seconds=0.)

    def _opponent_action(self, information, history, *, check_deadline=lambda: None):
        future = [event for event in history.deployments if event.tick > information.tick]
        first_tick = future[0].tick if future else history.root_tick + 1
        next_play = next((event for event in future if event.owner == information.owner), None)
        if next_play is not None and next_play.tick == first_tick:
            action = deployment_action(self.rollout, information, next_play)
            if action is not None:
                return action
            if next_play.tick <= information.tick + 60:
                raise IncompatibleHistory("public play has no legal reservation from the preceding boundary")
        cutoff = min(first_tick, history.root_tick)
        generator = torch.Generator().manual_seed(split_seed(self.seed, f"hidden-plan:{information.tick}"))
        for attempt in range(self.maximum_plan_attempts):
            check_deadline()
            action = self.opponent.sample(information, generator)
            if not information.legality.legal(action):
                raise ValueError("frozen opponent sampled an illegal own-view action")
            if action.card_id is None or information.tick + action.delay_slot * 2 > cutoff:
                if next_play is None or next_play.tick != first_tick or action.card_id is None:
                    return action
                if information.tick + action.delay_slot * 2 > first_tick:
                    return action
        raise IncompatibleHistory("bounded frozen-opponent history conditioning exhausted")

    def sync(self, history, information, *, check_deadline=lambda: None):
        if (self.closed or not isinstance(history, FairHistory) or not history.extends(self.history)
                or information.owner != self.owner or information.tick != history.root_tick
                or information.sha256 != history.root_fair_sha):
            raise ValueError("incremental FAIR history identity or prefix changed")
        started = time.monotonic()
        self.root.restore()
        previous = self.history
        decisions = {entry.tick: entry for entry in history.decisions[len(previous.decisions):]}
        deployments = {}
        for entry in history.deployments[len(previous.deployments):]:
            deployments.setdefault(entry.tick, []).append(entry)
        runtime = self.rollout.runtime
        try:
            while runtime.clock.tick < history.root_tick:
                check_deadline()
                frozen = self.rollout.freeze_due_information()
                if frozen:
                    actions = {}
                    for owner, current in frozen.items():
                        validate_fair_view(runtime.frozen[owner], owner)
                        if owner == self.owner:
                            entry = decisions.pop(current.tick, None)
                            if entry is None or current.sha256 != entry.fair_sha:
                                raise IncompatibleHistory(f"own historical FAIR boundary differs at {current.tick}")
                            actions[owner] = entry.action
                        else:
                            actions[owner] = self._opponent_action(current, history, check_deadline=check_deadline)
                    self.rollout.commit_simultaneous(actions)
                cursor = len(runtime.clock.evidence)
                self.rollout.advance()
                if runtime.failed or runtime.truncated or runtime.ended:
                    raise IncompatibleHistory("history terminated before its live root")
                if any(entry["reason"] == "rejected" for entry in runtime.clock.evidence[cursor:]):
                    raise IncompatibleHistory("history replay rejected a reservation")
                actual = sorted(PublicDeployment.from_event(event) for event in runtime.environment.action_outcomes()
                    if event.event_type == "action_executed" and all(
                        event_visible_to_owner(event, owner) for owner in (0, 1)))
                expected = deployments.pop(runtime.clock.tick, [])
                if actual != expected:
                    raise IncompatibleHistory(f"public execution evidence differs at {runtime.clock.tick}: "
                        f"expected={expected}, actual={actual}")
            root = self.rollout.freeze_due_information().get(self.owner)
            if decisions or deployments or root is None or root.sha256 != history.root_fair_sha:
                raise IncompatibleHistory("replayed root differs from full FAIR frame and legality")
            replacement = self.rollout.snapshot(self.sidecar_factory)
        except BaseException:
            self.root.restore()
            raise
        self.root.close()
        self.root, self.history = replacement, history
        ticks = history.root_tick - previous.root_tick
        self.profile["cold_ticks" if self.profile["syncs"] == 0 else "incremental_ticks"] += ticks
        self.profile["ticks"] += ticks
        self.profile["syncs"] += 1
        elapsed = time.monotonic() - started
        self.profile["seconds"] += elapsed
        self.profile["last_sync"] = dict(from_tick=previous.root_tick, to_tick=history.root_tick,
            ticks=ticks, seconds=elapsed)
        return root

    def close(self):
        if not self.closed:
            try:
                self.root.restore()
            finally:
                self.root.close()
                self.closed = True


class HistoryBeliefFactory:
    sampler_sha = semantic_digest(dict(schema="fair-final-history-belief-engineering.v1",
        prior="uniform-compatible-fixed-hog-initial-permutations", deck=list(HOG_DECK),
        execution="legal-reservations-only", hidden_plan="frozen-policy-conditioned-on-public-prefix"))

    def __init__(self, native_factory, *, catalog, opponent, sidecar_factory):
        self.native_factory, self.catalog = native_factory, catalog
        self.opponent, self.sidecar_factory = opponent, sidecar_factory

    def create(self, history, *, seed):
        if not isinstance(history, FairHistory):
            raise ValueError("belief initialization accepts only typed FAIR history")
        native = self.native_factory()
        try:
            engine_seed = split_seed(seed, "belief-engine") % (2**31 - 1)

            def match(deck0, deck1, match_seed):
                return MatchConfig(deck0=deck0, deck1=deck1, seed=match_seed,
                    end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs())

            decks, observed = NativeConditioner(native, match).create(
                OwnInitialDraw(history.opening.owner, history.opening.own_draw), seed=engine_seed,
                rng=random.Random(split_seed(seed, "own-draw")))
            opponent = 1 - history.opening.owner
            draw = compatible_opponent_draw(history, split_seed(seed, "opponent-draw"))
            _, slots = initial_draw(next(player for player in observed["players"] if player["owner"] == opponent))
            ordered = [None] * 8
            for slot, card in zip(slots, draw):
                ordered[slot] = card
            decks = list(decks)
            decks[opponent] = tuple(ordered)
            environment = FinalBattleEnv(native=native)
            environment.reset(match_config=match(*decks, engine_seed),
                options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                    allow_initial_remaining_runtime_rejections=True))
            if initial_draw(next(player for player in environment.raw_observation["players"]
                    if player["owner"] == opponent))[0] != draw:
                raise ValueError("conditioned opponent shuffle changed its semantics")
            native.start_atomic_steps()
            rollout = FinalRollout(environment, catalog=self.catalog, execution_seed=split_seed(seed, "rollout"))
            return HistoryReplaySession(rollout, opening=history.opening, opponent=self.opponent,
                seed=split_seed(seed, "opponent-history"), sidecar_factory=self.sidecar_factory)
        except BaseException:
            native.close_transport()
            raise

    def build(self, history, information, *, seed, attempts=4, check_deadline=lambda: None):
        if type(attempts) is not int or not 1 <= attempts <= 32:
            raise ValueError("belief rebuild requires a bounded attempt count")
        failures = []
        for attempt in range(attempts):
            check_deadline()
            session = None
            try:
                session = self.create(history, seed=split_seed(seed, f"attempt:{attempt}"))
                session.sync(history, information, check_deadline=check_deadline)
                return session
            except IncompatibleHistory as error:
                failures.append(str(error))
                if session is not None:
                    try:
                        session.close()
                    finally:
                        session.rollout.runtime.environment.native.close_transport()
            except BaseException:
                if session is not None:
                    try:
                        session.close()
                    finally:
                        session.rollout.runtime.environment.native.close_transport()
                raise
        raise IncompatibleHistory("no compatible independent world: " + "; ".join(failures))
