from dataclasses import dataclass
import json
import random
import time

from .config import HOG_DECK


class IncompatibleBelief(ValueError):
    pass


@dataclass(frozen=True)
class OwnInitialDraw:
    owner: int
    known_cards: tuple[int, ...]

    def __post_init__(self):
        if self.owner not in (0, 1) or not 5 <= len(self.known_cards) <= 8:
            raise ValueError("conditioned initialization needs own four hand cards and next card")
        if len(set(self.known_cards)) != len(self.known_cards) or not set(self.known_cards) <= set(HOG_DECK):
            raise ValueError("own initial draw is not a Hog prefix")


def known_own_draw(information, catalog):
    entries = [json.loads(record) for record in information.own_history]
    if not entries or "initial_draw" not in entries[0]:
        raise ValueError("missing own initial draw history")
    cards = list(entries[0]["initial_draw"])
    for entry in entries[1:]:
        card = entry.get("next_card")
        if card in HOG_DECK and card not in cards:
            cards.append(card)
    own = information.frame.own_cards
    next_rows = [index for index in range(own.mask.shape[1])
                 if bool(own.mask[0, index]) and float(own.runtime_features[0, index, 1]) == 1.]
    if len(next_rows) != 1:
        raise ValueError("FAIR root must identify exactly one own next card")
    vocab = int(own.card_vocab_id[0, next_rows[0]])
    matches = [card for card in HOG_DECK if catalog.vocab_id(card) == vocab]
    if len(matches) != 1:
        raise ValueError("own next card is outside the fixed deck")
    if matches[0] not in cards:
        cards.append(matches[0])
    return OwnInitialDraw(information.owner, tuple(cards))


def initial_draw(player):
    hand = sorted(player["hand"], key=lambda item: item["handIndex"])
    cycle = sorted(player["cycle"], key=lambda item: item["cycleIndex"])
    if len(hand) != 4 or len(cycle) != 4:
        raise ValueError("native initial draw shape differs")
    cards = tuple(item["cardId"] for item in hand + cycle)
    slots = tuple(item["deckSlot"] for item in hand + cycle)
    if set(cards) != set(HOG_DECK) or set(slots) != set(range(8)):
        raise ValueError("native shuffle is not a base Hog permutation")
    return cards, slots


class NativeConditioner:
    def __init__(self, native, match_factory):
        self.native, self.match_factory = native, match_factory

    def create(self, own_draw, *, seed, rng, required_initial_cards=None, public_play_sequences=None):
        public_play_sequences = public_play_sequences or {}
        for owner, sequence in public_play_sequences.items():
            if owner not in (0, 1) or owner == own_draw.owner or not sequence:
                raise ValueError("invalid public play sequence owner or history")
            if any(card not in HOG_DECK for card in sequence):
                raise ValueError("public play sequence is outside the deck")
        reference = self.native.create_match(self.match_factory(HOG_DECK, HOG_DECK, seed))
        sampled_player = next(player for player in reference["players"] if player["owner"] == own_draw.owner)
        _, permutation = initial_draw(sampled_player)
        remainder = sorted(set(HOG_DECK) - set(own_draw.known_cards))
        rng.shuffle(remainder)
        wanted = (*own_draw.known_cards, *remainder)
        ordered = [None] * 8
        for slot, card in zip(permutation, wanted):
            ordered[slot] = card
        decks = [HOG_DECK, HOG_DECK]
        decks[own_draw.owner] = tuple(ordered)
        required_initial_cards = required_initial_cards or {}
        for owner, card_id in required_initial_cards.items():
            if owner not in (0, 1) or owner == own_draw.owner:
                raise ValueError("invalid conditioned public card owner")
            if card_id not in HOG_DECK:
                raise ValueError("conditioned public card is outside the deck")
            sampled_player = next(player for player in reference["players"] if player["owner"] == owner)
            _, slots = initial_draw(sampled_player)
            candidate = list(HOG_DECK)
            rng.shuffle(candidate)
            target_slot = rng.choice(slots[:4])
            candidate[candidate.index(card_id)], candidate[target_slot] = candidate[target_slot], card_id
            decks[owner] = tuple(candidate)
        for owner, sequence in public_play_sequences.items():
            sampled_player = next(player for player in reference["players"] if player["owner"] == owner)
            _, slots = initial_draw(sampled_player)
            draw = tuple(decks[owner][slot] for slot in slots)
            if not public_draw_compatible(draw, sequence):
                raise ValueError("public replay card is absent in the sampled draw sequence")
        observation = self.native.create_match(self.match_factory(*decks, seed))
        player = next(player for player in observation["players"] if player["owner"] == own_draw.owner)
        cards, observed_permutation = initial_draw(player)
        if cards != wanted or observed_permutation != permutation:
            raise ValueError("deck permutation changed native shuffle semantics")
        for owner, card_id in required_initial_cards.items():
            player = next(player for player in observation["players"] if player["owner"] == owner)
            if card_id not in initial_draw(player)[0][:4]:
                raise ValueError("public first play was not conditioned into the initial hand")
        return tuple(decks), observation


def public_draw_compatible(draw, sequence):
    hand = set(draw[:4])
    cycle = list(draw[4:])
    for card in sequence:
        if card not in hand:
            return False
        hand.remove(card)
        hand.add(cycle.pop(0))
        cycle.append(card)
    return True


class ReplayBeliefProvider:
    def __init__(self, factory, *, attempts=32):
        if attempts < 1:
            raise ValueError("belief rebuild attempts must be positive")
        self.factory, self.attempts = factory, attempts
        self.profile = dict(attempts=0, accepted=0, rejected=0, errors=[])

    def build(self, information, *, seed, limit):
        rng = random.Random(seed)
        accepted = []
        try:
            for ordinal in range(self.attempts):
                if len(accepted) >= limit:
                    break
                world = None
                self.profile["attempts"] += 1
                try:
                    world = self.factory(information, seed=rng.randrange(2**32), ordinal=ordinal)
                    world.restore_root()
                    if world.root_information().sha256 != information.sha256:
                        raise ValueError("conditional replay changed FAIR information")
                    accepted.append(world)
                    self.profile["accepted"] += 1
                except ValueError as error:
                    self.profile["rejected"] += 1
                    self.profile["errors"].append(str(error))
                    del self.profile["errors"][:-8]
                    if world is not None:
                        world.close()
            if not accepted:
                recent = "; ".join(self.profile["errors"][-4:])
                raise ValueError("no compatible replay world; actual private state is not a fallback"
                                 + (f": {recent}" if recent else ""))
            return accepted
        except BaseException:
            for world in accepted:
                world.close()
            raise


class _WorldLease:
    def __init__(self, provider, world):
        self.provider, self.world = provider, world
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.world, name)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.world.restore_root()
        except BaseException:
            self.provider.worlds.remove(self.world)
            self.world.close()
            raise
        finally:
            self.provider.leased.discard(id(self.world))


class IncrementalBeliefProvider(ReplayBeliefProvider):
    def __init__(self, factory, *, attempts=32, reserve=0):
        if type(reserve) is not int or not 0 <= reserve < 8:
            raise ValueError("incremental belief reserve must be an integer between zero and seven")
        super().__init__(factory, attempts=attempts)
        self.reserve = reserve
        self.worlds = []
        self.active_worlds = []
        self.leased = set()
        self.owner = None
        self.profile.update(reused=0, invalidated=0, incremental_ticks=0,
                            incremental_seconds=0., rebuild_seconds=0.,
                            reserve_reused=0, reserve_invalidated=0,
                            rebuild_avoided=0, active_worlds=0, reserve_worlds=0)
        self.profile.update(conditioned_roots=0, conditioned_seconds=0.)

    def build(self, information, *, seed, limit):
        if limit < 1 or self.leased:
            raise ValueError("incremental belief requires a positive limit and no active search")
        if type(limit) is not int or limit + self.reserve > 8:
            raise ValueError("active and reserve belief worlds must total at most eight")
        if self.owner is not None and self.owner != information.owner:
            raise ValueError("incremental belief cannot share private information between owners")
        self.owner = information.owner
        had_pool = bool(self.worlds)
        previous_active = {id(world) for world in self.active_worlds}
        previous_reserve = {id(world) for world in self.worlds if id(world) not in previous_active}
        accepted = []
        try:
            for world in tuple(self.worlds):
                previous_tick = world.root_information().tick
                started = time.perf_counter()
                try:
                    self.factory.update(world, information)
                except IncompatibleBelief as error:
                    self.profile["invalidated"] += 1
                    self.profile["errors"].append(str(error))
                    del self.profile["errors"][:-8]
                    if id(world) not in previous_active:
                        self.profile["reserve_invalidated"] += 1
                    self.worlds.remove(world)
                    world.close()
                else:
                    accepted.append(world)
                    self.profile["reused"] += 1
                    self.profile["incremental_ticks"] += information.tick - previous_tick
                finally:
                    self.profile["incremental_seconds"] += time.perf_counter() - started
            active = [world for world in accepted if id(world) in previous_active]
            active.extend(world for world in accepted if id(world) not in previous_active)
            if len(active) < limit:
                started = time.perf_counter()
                try:
                    try:
                        required = (limit + self.reserve) if not had_pool else limit - len(active)
                        added = super().build(information, seed=seed, limit=required)
                    except ValueError:
                        if not active:
                            raise
                        added = []
                finally:
                    self.profile["rebuild_seconds"] += time.perf_counter() - started
                self.worlds.extend(added)
                accepted.extend(added)
                active.extend(added)
            if not active:
                raise ValueError("incremental belief could not replenish active worlds")

            selected = active[:limit]
            promoted = sum(1 for world in selected if id(world) in previous_reserve)
            if promoted:
                self.profile["reserve_reused"] += promoted
                self.profile["rebuild_avoided"] += promoted

            selected_ids = {id(world) for world in selected}
            self.active_worlds = selected
            self.worlds = [*selected, *[world for world in accepted if id(world) not in selected_ids]]
            self.profile.update(active_worlds=len(selected), reserve_worlds=len(self.worlds) - len(selected))
            self.profile.update(conditioned_roots=getattr(self.factory, "conditioned_roots", 0),
                                conditioned_seconds=getattr(self.factory, "conditioned_seconds", 0.))
            self.leased = selected_ids
            return [_WorldLease(self, world) for world in selected]
        except BaseException:
            self.close()
            raise

    def close(self):
        worlds, self.worlds = self.worlds, []
        self.active_worlds = []
        self.profile.update(active_worlds=0, reserve_worlds=0)
        self.leased.clear()
        failures = []
        for world in worlds:
            try:
                world.close()
            except Exception as error:
                failures.append(error)
        if failures:
            raise RuntimeError("incremental belief cleanup failed") from failures[0]
