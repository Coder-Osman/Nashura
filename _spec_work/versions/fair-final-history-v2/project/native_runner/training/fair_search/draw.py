from itertools import permutations
import json

import numpy as np

from .config import HOG_DECK


def refill_delay(tick):
    if tick <= 2400:
        return 20
    if tick <= 4800:
        return 10
    return 7


class ConditionalDraws:
    def __init__(self, owner, initial_cards):
        self.owner = owner
        prefix = tuple(initial_cards)
        if owner not in (0, 1) or not 5 <= len(prefix) <= 8 or len(set(prefix)) != len(prefix):
            raise ValueError("conditional own draw prefix differs")
        if not set(prefix) <= set(HOG_DECK):
            raise ValueError("conditional draw contains an unknown card")
        remaining = tuple(card for card in HOG_DECK if card not in prefix)
        own = [(*prefix, *tail) for tail in permutations(remaining)]
        opponent = list(permutations(HOG_DECK))
        self.initial = {owner: np.asarray(own, dtype=np.int64),
                        1 - owner: np.asarray(opponent, dtype=np.int64)}
        self.current = {side: values.copy() for side, values in self.initial.items()}
        self.public_cursor = 0
        self.own_cursor = 1
        self.play_counts = {0: 0, 1: 0}
        self.next_refill = {0: 0, 1: 0}
        self.information = None

    def propose(self, information, catalog, *, rng):
        if information.owner != self.owner:
            raise ValueError("conditional draw owner differs")
        if self.information is not None and (
                information.tick < self.information.tick
                or not information.public_history.extends(self.information.public_history)
                or not information.own_history.extends(self.information.own_history)):
            raise ValueError("conditional draw information does not extend its previous root")
        initial = dict(self.initial)
        previous = {side: values.copy() for side, values in self.current.items()}
        current = {side: values.copy() for side, values in self.current.items()}
        play_counts = dict(self.play_counts)
        next_refill = dict(self.next_refill)

        def advance(side, tick):
            values = current[side]
            while values.shape[1] > 8 and next_refill[side] <= tick:
                hand_index = np.argmax(values[:, :4] == -1, axis=1)
                values[np.arange(len(values)), hand_index] = values[:, 4]
                values = np.concatenate((values[:, :4], values[:, 5:]), axis=1)
                next_refill[side] += refill_delay(next_refill[side])
            current[side] = values

        def filter_rows(side, keep, evidence):
            initial[side] = initial[side][keep]
            previous[side] = previous[side][keep]
            current[side] = current[side][keep]
            if not len(current[side]):
                raise ValueError("no draw permutation is compatible with FAIR history: "
                                 f"owner={side}, root_tick={information.tick}, evidence={evidence}")

        observations = [(event.tick, 0, event) for event in information.public_history[self.public_cursor:]]
        observations.extend((entry["tick"], 1, entry) for entry in
                            (json.loads(record) for record in information.own_history[self.own_cursor:]))
        for _, kind, observation in sorted(observations, key=lambda item: item[:2]):
            if kind == 1:
                advance(self.owner, observation["tick"])
                next_card = observation.get("next_card")
                if next_card not in HOG_DECK:
                    raise ValueError("conditional own decision needs an observed next card")
                filter_rows(self.owner, current[self.owner][:, 4] == next_card,
                            f"decision_next_card@{observation['tick']}")
                continue
            side = observation.owner
            advance(side, observation.tick - 1)
            values = current[side]
            present = np.any(values[:, :4] == observation.card_id, axis=1)
            filter_rows(side, present, f"public_play@{observation.tick}")
            values = current[side]
            hand_index = np.argmax(values[:, :4] == observation.card_id, axis=1)
            values[np.arange(len(values)), hand_index] = -1
            current[side] = np.concatenate((values, np.full((len(values), 1), observation.card_id)), axis=1)
            next_refill[side] = max(next_refill[side], observation.tick)
            advance(side, observation.tick)
            play_counts[side] += 1

        for side in (0, 1):
            advance(side, information.tick)

        own = information.frame.own_cards
        mapping = {catalog.vocab_id(card): card for card in HOG_DECK}
        hand, next_cards = [], []
        for row in range(own.mask.shape[1]):
            if not bool(own.mask[0, row]):
                continue
            card = mapping.get(int(own.card_vocab_id[0, row]))
            if card is None:
                raise ValueError("conditional FAIR own card is outside the fixed deck")
            if float(own.runtime_features[0, row, 0]) == 1.:
                hand.append(card)
            if float(own.runtime_features[0, row, 1]) == 1.:
                next_cards.append(card)
        if not 0 <= len(hand) <= 4 or len(next_cards) != 1:
            raise ValueError(f"conditional FAIR own hand or next card differs: tick={information.tick}, hand={hand}, next={next_cards}")
        values = current[self.owner]
        filter_rows(self.owner, (np.sort(values[:, :4], axis=1) == sorted([-1] * (4 - len(hand)) + hand)).all(axis=1)
                    & (values[:, 4] == next_cards[0]), "current_hand_and_next")
        selected = {side: rng.randrange(len(values)) for side, values in current.items()}
        queues = {side: tuple(map(int, previous[side][selected[side]])) for side in (0, 1)}
        result = object.__new__(type(self))
        result.owner, result.initial, result.current = self.owner, initial, current
        result.public_cursor, result.own_cursor = len(information.public_history), len(information.own_history)
        result.play_counts = play_counts
        result.next_refill = next_refill
        result.information = information
        result.selected_initial = {side: tuple(map(int, initial[side][selected[side]])) for side in (0, 1)}
        return result, queues

    def initial_queues(self, *, rng):
        return {side: tuple(map(int, values[rng.randrange(len(values))]))
                for side, values in self.initial.items()}
