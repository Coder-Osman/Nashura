from collections import Counter
from dataclasses import dataclass
from itertools import permutations
import random

import numpy as np

from ..fair_search.contracts import require_sha, semantic_digest
from .contracts import exact_fields
from .visibility import event_visible_to_owner


RULES = "eight-distinct-base-four-hand-fifo.v1"
ORDER_COUNT = 40320
MAXIMUM_DECKS = 24


def validate_deck(cards):
    if (type(cards) is not tuple or len(cards) != 8 or len(set(cards)) != 8
            or any(type(card) is not int or not 0 < card < 2**31 for card in cards)
            or cards != tuple(sorted(cards))):
        raise ValueError("deck requires eight canonical distinct base card identities")


@dataclass(frozen=True)
class DeckFrequency:
    cards: tuple
    count: int

    def __post_init__(self):
        validate_deck(self.cards)
        if type(self.count) is not int or self.count < 1:
            raise ValueError("deck frequency must be a positive integer")


@dataclass(frozen=True)
class PublicPlay:
    tick: int
    card_id: int

    def __post_init__(self):
        if (type(self.tick) is not int or self.tick < 0 or type(self.card_id) is not int
                or not 0 < self.card_id < 2**31):
            raise ValueError("invalid public card execution")

    @classmethod
    def from_event(cls, event, *, owner):
        if (type(owner) is not int or owner not in (0, 1) or event.owner != owner
                or event.event_type != "action_executed"
                or not all(event_visible_to_owner(event, observer) for observer in (0, 1))):
            raise ValueError("only public executed opponent cards enter cycle evidence")
        return cls(event.tick, event.card_id)


@dataclass(frozen=True)
class PublicDeckRecord:
    game_sha: str
    owner: int
    plays: tuple

    def __post_init__(self):
        require_sha(self.game_sha)
        if type(self.owner) is not int or self.owner not in (0, 1):
            raise ValueError("invalid public deck record owner")
        validate_plays(self.plays)
        validate_deck(tuple(sorted({play.card_id for play in self.plays})))


def validate_plays(plays):
    if type(plays) is not tuple or any(not isinstance(play, PublicPlay) for play in plays):
        raise ValueError("public cycle requires typed immutable executions")
    ticks = [play.tick for play in plays]
    if ticks != sorted(set(ticks)):
        raise ValueError("public executions must be ordered and unique")


@dataclass(frozen=True)
class FrozenDeckPrior:
    kind: str
    source_sha: str
    entries: tuple

    schema = "fair-final-deck-prior.v1"

    def __post_init__(self):
        require_sha(self.source_sha)
        if self.kind not in ("fixed-experiment", "empirical-complete-public"):
            raise ValueError("unknown deck prior source kind")
        if (type(self.entries) is not tuple or not 1 <= len(self.entries) <= MAXIMUM_DECKS
                or any(not isinstance(entry, DeckFrequency) for entry in self.entries)):
            raise ValueError("bounded immutable deck prior required")
        cards = [entry.cards for entry in self.entries]
        if cards != sorted(set(cards)):
            raise ValueError("deck prior support must be canonical and unique")

    def to_payload(self):
        return dict(schema=self.schema, rules=RULES, kind=self.kind, source_sha=self.source_sha,
            entries=[dict(cards=list(entry.cards), count=entry.count) for entry in self.entries])

    @property
    def sha256(self):
        return semantic_digest(self.to_payload())

    @classmethod
    def from_payload(cls, payload):
        exact_fields(payload, {"schema", "rules", "kind", "source_sha", "entries"}, "deck prior")
        if payload["schema"] != cls.schema or payload["rules"] != RULES or type(payload["entries"]) is not list:
            raise ValueError("deck prior version or rules differ")
        entries = []
        for entry in payload["entries"]:
            exact_fields(entry, {"cards", "count"}, "deck frequency")
            if type(entry["cards"]) is not list:
                raise ValueError("deck cards must be an identity list")
            entries.append(DeckFrequency(tuple(entry["cards"]), entry["count"]))
        return cls(payload["kind"], payload["source_sha"], tuple(entries))

    @classmethod
    def from_public_records(cls, records, *, source_sha):
        seen, counts = {}, Counter()
        for record in records:
            if not isinstance(record, PublicDeckRecord):
                raise ValueError("empirical prior requires public-only complete deck records")
            key = record.game_sha, record.owner
            if key in seen:
                if seen[key] != record:
                    raise ValueError("conflicting duplicate public game record")
                continue
            cards = tuple(sorted({play.card_id for play in record.plays}))
            diagnostic_prior = cls("fixed-experiment", source_sha, (DeckFrequency(cards, 1),))
            cycle = PublicCycleBelief(diagnostic_prior, owner=record.owner, opening_tick=0)
            cycle.advance(record.plays, from_tick=0, to_tick=record.plays[-1].tick,
                expected_evidence_sha=cycle.evidence_sha)
            seen[key] = record
            counts[cards] += 1
        return cls("empirical-complete-public", source_sha, tuple(
            DeckFrequency(cards, count) for cards, count in sorted(counts.items())))


class IncompatiblePublicCycle(ValueError):
    pass


@dataclass(frozen=True)
class DrawCompletion:
    deck: tuple
    initial_draw: tuple
    current_draw: tuple
    manifest: dict


class PublicCycleBelief:
    def __init__(self, prior, *, owner, opening_tick):
        if (not isinstance(prior, FrozenDeckPrior) or type(owner) is not int or owner not in (0, 1)
                or type(opening_tick) is not int or opening_tick < 0):
            raise ValueError("cycle belief requires frozen prior and owner identity")
        self.prior, self.owner, self.tick = prior, owner, opening_tick
        self.prior_sha = prior.sha256
        self.evidence_sha = semantic_digest(dict(schema="fair-final-public-cycle-chain.v1",
            owner=owner, opening_tick=opening_tick, rules=RULES))
        self.sampler_sha = semantic_digest(dict(schema="fair-final-public-cycle-sampler.v1",
            prior_sha=self.prior_sha, rules=RULES, initial="uniform-permutation-within-deck",
            conditioning="empirical-count-times-compatible-order-count",
            scope="card-cycle-only-not-full-world-posterior"))
        self._support = []
        for entry in prior.entries:
            initial = np.asarray(list(permutations(entry.cards)), dtype=np.int64)
            self._support.append((entry, initial, initial.copy()))
        self.play_count = self.transition_work = 0

    @property
    def memory_bytes(self):
        return sum(initial.nbytes + current.nbytes for _, initial, current in self._support)

    def advance(self, plays, *, from_tick, to_tick, expected_evidence_sha):
        validate_plays(plays)
        if (type(from_tick) is not int or type(to_tick) is not int or from_tick != self.tick
                or to_tick < from_tick or expected_evidence_sha != self.evidence_sha
                or any(not from_tick < play.tick <= to_tick for play in plays)):
            raise ValueError("public cycle delta has stale identity or invalid tick bounds")
        replacement = []
        work = 0
        for entry, initial, current in self._support:
            for play in plays:
                work += len(current)
                keep = (current[:, :4] == play.card_id).any(axis=1)
                initial, current = initial[keep], current[keep]
                if not len(current):
                    break
                slots = (current[:, :4] == play.card_id).argmax(axis=1)
                current[np.arange(len(current)), slots] = current[:, 4]
                current[:, 4:7] = current[:, 5:8].copy()
                current[:, 7] = play.card_id
            if len(current):
                replacement.append((entry, initial, current))
        if not replacement:
            raise IncompatiblePublicCycle("no prior-supported order matches public executions")
        digest = self.evidence_sha
        for play in plays:
            digest = semantic_digest([digest, play.tick, play.card_id])
        self._support, self.evidence_sha, self.tick = replacement, digest, to_tick
        self.play_count += len(plays)
        self.transition_work += work
        return dict(added_plays=len(plays), order_checks=work, compatible_orders=sum(
            len(current) for _, _, current in self._support), evidence_sha=digest)

    def posterior(self):
        total = sum(entry.count * len(current) for entry, _, current in self._support)
        return tuple(dict(deck=entry.cards, compatible_orders=len(current),
            mass_numerator=entry.count * len(current), mass_denominator=total)
            for entry, _, current in self._support)

    def support_digest(self):
        return semantic_digest([dict(deck=entry.cards, count=entry.count,
            initial_bytes=initial.tobytes().hex(), current_bytes=current.tobytes().hex())
            for entry, initial, current in self._support])

    def sample(self, *, seed):
        if type(seed) is not int or seed < 0:
            raise ValueError("cycle completion requires an independent nonnegative seed")
        total = sum(entry.count * len(current) for entry, _, current in self._support)
        ticket = random.Random(seed).randrange(total)
        for entry, initial, current in self._support:
            mass = entry.count * len(current)
            if ticket < mass:
                index = ticket // entry.count
                initial_draw = tuple(map(int, initial[index]))
                current_draw = tuple(map(int, current[index]))
                manifest = dict(prior_sha=self.prior_sha, sampler_sha=self.sampler_sha,
                    public_evidence_sha=self.evidence_sha, owner=self.owner, tick=self.tick,
                    seed_commitment=semantic_digest([seed]),
                    completion_commitment=semantic_digest([entry.cards, initial_draw, current_draw]),
                    scope="card-cycle-only", training_admissible=False)
                return DrawCompletion(entry.cards, initial_draw, current_draw, manifest)
            ticket -= mass
        raise AssertionError("integer prior sampler exhausted its support")
