"""FAIR resource bounds and revealed-card cycles without an opponent deck."""
from __future__ import annotations

from ..training.tracking import AvailabilityIndex, PublicCardState
from .events import PublicEventCursor
from .snapshot import LiveStateError


class LivePublicTracker:
    """Implements the tensorizer's public tracker interface from actual plays.

    No opponent private state or fabricated initial hand is accepted. Resource
    lower bounds remain zero; the upper bound subtracts observed public costs.
    Unknown incoming elixir mechanics widen that bound rather than inventing
    exact information. Card cycles remain unknown after ambiguous descriptors
    or Champion/hero plays with special cycling semantics.
    """

    def __init__(self, *, owner: int, card_specs, timeline, required: bool = True):
        self.owner, self.card_specs, self.timeline = owner, card_specs, timeline
        self.cursor = PublicEventCursor(required=required)
        self.tick = None
        self.upper = 10.0
        self.revealed = {0: [], 1: []}
        self.plays = {0: 0, 1: 0}
        self.last_play = {0: {}, 1: {}}
        self.cycles_known = True
        self.resource_bound_known = True
        self.own_elixir = 0.0

    def _advance(self, tick):
        if self.tick is None:
            self.tick = tick
        if tick < self.tick:
            raise LiveStateError("public tracker tick regressed")
        while self.tick < tick:
            boundary = min(tick, self.timeline.next_elixir_boundary(self.tick) or tick)
            rate = 100000 * 50 // self.timeline.elixir_rate(self.tick).full_bar_ms
            self.upper = min(10., self.upper + (boundary-self.tick)*rate/10000)
            self.tick = boundary

    def consume(self, row):
        fresh = self.cursor.update(row)
        if self.tick is None:
            self.tick = row.get("public_events", {}).get("started_tick", row["tick"])
        for event in fresh:
            self._advance(event["tick"])
            owner, card = event["owner"], event["card_id"]
            spec = self.card_specs.get(card)
            if spec is None:
                # Still a real scheduling event, but not a guessed card token.
                if owner != self.owner:
                    self.cycles_known = False
                    self.resource_bound_known = False
                continue
            identity_known = (event["card_identity"] == "own-deck"
                              or spec.elixir_cost == event["cost"])
            if owner != self.owner:
                self.upper = max(0., self.upper-event["cost"])
                if not identity_known or event["form_code"] >= 2:
                    self.cycles_known = False
                previous = self.last_play[owner].get(card)
                if previous is not None and self.plays[owner]-previous < 4:
                    self.cycles_known = False
            # Mirror's consumed descriptor names the copied card. Do not report
            # it as a second root-card play merely because it shares an ID.
            if identity_known:
                if card not in self.revealed[owner]:
                    self.revealed[owner].append(card)
                self.plays[owner] += 1
                self.last_play[owner][card] = self.plays[owner]
            if card in (27000007, 26000067):  # Elixir Collector / Elixir Golem.
                self.resource_bound_known = False
        if any(e["card_id"] in (27000007, 26000067) for e in row["entities"]):
            self.resource_bound_known = False
        self._advance(row["tick"])
        self.own_elixir = row["own"]["elixir_raw"] / 10000
        return fresh

    def update(self, observation):
        # consume() runs once for every capture frame before tensorization.
        if self.tick != observation.tick or observation.owner != self.owner:
            raise LiveStateError("public tracker is not aligned with observation")

    def reconcile_owner_private_state(self, observation):
        self.update(observation)

    def revealed_cards(self, owner):
        return tuple(self.revealed[owner])

    def card_state(self, owner, card_id):
        last = self.last_play[owner].get(card_id)
        if last is None or (owner != self.owner and not self.cycles_known):
            return PublicCardState(AvailabilityIndex.UNKNOWN_INITIAL, 0, False)
        distance = max(0, 4-(self.plays[owner]-last))
        return PublicCardState(AvailabilityIndex.CYCLING if distance else AvailabilityIndex.AVAILABLE,
                               distance, True)

    def elixir_interval(self, owner):
        if owner == self.owner:
            return self.own_elixir, self.own_elixir
        return 0., self.upper if self.resource_bound_known else 10.

    def ability_state(self, *args, **kwargs):
        # No verified live ability activation receipts yet.
        return None

    def end_episode(self):
        self.tick = None
