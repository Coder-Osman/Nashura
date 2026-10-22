import math

import numpy as np

from native_runner.training.fair_final.deck_belief import (
    IncompatiblePublicCycle, PublicCycleBelief, validate_plays,
)
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.draw import refill_delay


class DelayedCycleAudit:
    def __init__(self, prior, *, owner, opening_tick):
        base = PublicCycleBelief(prior, owner=owner, opening_tick=opening_tick)
        self.prior, self.owner, self.tick = prior, owner, opening_tick
        self._support = base._support
        self.next_refill = 0
        self.evidence_sha = semantic_digest(dict(schema="delayed-public-cycle-audit.v1",
            prior_sha=prior.sha256, owner=owner, opening_tick=opening_tick,
            timing="public-execution-before-same-tick-refill;20-10-7-ticks",
            training_admissible=False))

    def advance(self, plays, *, from_tick, to_tick, expected_evidence_sha):
        validate_plays(plays)
        if (type(from_tick) is not int or type(to_tick) is not int or from_tick != self.tick
                or to_tick < from_tick or expected_evidence_sha != self.evidence_sha
                or any(not from_tick < play.tick <= to_tick for play in plays)):
            raise ValueError("delayed cycle delta has stale identity or invalid tick bounds")
        replacement = []
        final_refill = None
        for entry, original, source in self._support:
            initial, current = original.copy(), source.copy()
            refill_tick = self.next_refill

            def advance_refills(tick):
                nonlocal current, refill_tick
                while current.shape[1] > 8 and refill_tick <= tick:
                    slots = (current[:, :4] == -1).argmax(axis=1)
                    current[np.arange(len(current)), slots] = current[:, 4]
                    current = np.concatenate((current[:, :4], current[:, 5:]), axis=1)
                    refill_tick += refill_delay(refill_tick)

            for play in plays:
                advance_refills(play.tick - 1)
                keep = (current[:, :4] == play.card_id).any(axis=1)
                initial, current = initial[keep], current[keep]
                if not len(current):
                    break
                slots = (current[:, :4] == play.card_id).argmax(axis=1)
                current[np.arange(len(current)), slots] = -1
                current = np.concatenate((current, np.full((len(current), 1), play.card_id)), axis=1)
                refill_tick = max(refill_tick, play.tick)
                advance_refills(play.tick)
            if not len(current):
                continue
            advance_refills(to_tick)
            if final_refill is not None and final_refill != refill_tick:
                raise ValueError("candidate refill clocks disagree")
            final_refill = refill_tick
            replacement.append((entry, initial, current))
        if not replacement:
            raise IncompatiblePublicCycle("no delayed order matches public executions")
        evidence = self.evidence_sha
        for play in plays:
            evidence = semantic_digest([evidence, play.tick, play.card_id])
        self._support, self.next_refill = replacement, final_refill
        self.tick, self.evidence_sha = to_tick, evidence

    def predict(self):
        cards = sorted({card for entry, _, _ in self._support for card in entry.cards})
        total = sum(entry.count * len(current) for entry, _, current in self._support)
        hand = {card: sum(entry.count * int((current[:, :4] == card).any(1).sum())
            for entry, _, current in self._support) / total for card in cards}
        upcoming = {card: sum(entry.count * int((current[:, 4] == card).sum())
            for entry, _, current in self._support) / total for card in cards}
        holes = {current.shape[1] - 8 for _, _, current in self._support}
        if (len(holes) != 1 or not math.isclose(sum(upcoming.values()), 1.)
                or not math.isclose(sum(hand.values()), 4 - next(iter(holes)))):
            raise ValueError("delayed marginals violate card conservation")
        return dict(hand_inclusion=hand, next_card=upcoming,
            holes=next(iter(holes)), orders=sum(len(current) for _, _, current in self._support))

    def score(self, hand, upcoming):
        prediction = self.predict()
        cards = sorted(prediction["next_card"])
        if (len(hand) > 4 or len(hand) != len(set(hand)) or set(hand) - set(cards)
                or upcoming not in cards or upcoming in hand):
            raise ValueError("audit labels violate fixed-deck hand and next rules")
        total, compatible = 0, 0
        for entry, _, current in self._support:
            total += entry.count * len(current)
            expected = sorted([-1] * (4 - len(hand)) + list(hand))
            matches = (np.sort(current[:, :4], axis=1) == expected).all(axis=1)
            compatible += entry.count * int((matches & (current[:, 4] == upcoming)).sum())
        probability = prediction["next_card"][upcoming]
        return dict(next_brier=sum((prediction["next_card"][card] - (card == upcoming))**2 for card in cards),
            hand_brier=sum((prediction["hand_inclusion"][card] - (card in hand))**2 for card in cards),
            next_nll=None if probability == 0 else -math.log(probability),
            zero_next_probability=probability == 0, truth_in_support=compatible > 0,
            joint_compatible_probability_mass=compatible / total)

    def support_digest(self):
        return semantic_digest(dict(tick=self.tick, next_refill=self.next_refill,
            evidence_sha=self.evidence_sha, support=[dict(deck=entry.cards, count=entry.count,
                initial=initial.tobytes().hex(), current=current.tobytes().hex(), shape=current.shape)
                for entry, initial, current in self._support]))
