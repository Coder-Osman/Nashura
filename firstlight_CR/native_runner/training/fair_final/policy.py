from dataclasses import dataclass
import math

import torch


@dataclass(frozen=True, order=True)
class FullAction:
    card_id: int | None = None
    cell: int | None = None
    delay_slot: int | None = None

    def __post_init__(self):
        if self.card_id is None:
            if self.cell is not None or self.delay_slot is not None:
                raise ValueError("WAIT has no ACT components")
        elif (type(self.card_id) is not int or self.card_id <= 0 or type(self.cell) is not int
              or not 0 <= self.cell < 576 or type(self.delay_slot) is not int or not 1 <= self.delay_slot <= 30):
            raise ValueError("invalid stable card, cell, or executable delay")

    @property
    def key(self):
        return "wait" if self.card_id is None else f"act:{self.card_id}:{self.cell}:{self.delay_slot}"


class LegalPrefixes:
    def __init__(self, card_ids, legal_plays, *, wait=True):
        self.card_ids = tuple(card_ids)
        if (not self.card_ids or len(set(self.card_ids)) != len(self.card_ids)
                or any(type(card) is not int or card <= 0 for card in self.card_ids)
                or legal_plays.dtype != torch.bool or legal_plays.ndim != 3
                or legal_plays.shape[0] != len(self.card_ids) or not 1 <= legal_plays.shape[1] <= 576
                or legal_plays.shape[2] != 31 or type(wait) is not bool):
            raise ValueError("invalid legal-prefix contract")
        self.plays = legal_plays.clone()
        self.plays[..., 0] = False
        self.cells = self.plays.any(-1)
        self.cards = self.cells.any(-1)
        self.modes = torch.tensor([wait, bool(self.cards.any())], device=legal_plays.device)
        if not bool(self.modes.any()):
            raise ValueError("decision has no legal action")

    def mask(self, prefix):
        if not prefix:
            return self.modes
        if prefix == (1,):
            return self.cards
        if len(prefix) not in (2, 3) or prefix[0] != 1:
            raise ValueError("invalid ACT prefix")
        card = prefix[1]
        if type(card) is not int or not 0 <= card < len(self.card_ids) or not bool(self.cards[card]):
            raise ValueError("prefix has no legal completion")
        if len(prefix) == 2:
            return self.cells[card]
        cell = prefix[2]
        if type(cell) is not int or not 0 <= cell < self.plays.shape[1] or not bool(self.cells[card, cell]):
            raise ValueError("prefix has no legal completion")
        return self.plays[card, cell]

    def legal(self, action):
        if action.card_id is None:
            return bool(self.modes[0])
        if action.card_id not in self.card_ids or action.cell >= self.plays.shape[1]:
            return False
        return bool(self.plays[self.card_ids.index(action.card_id), action.cell, action.delay_slot])


class JointPolicy:
    mask_semantics = "prefix-feasibility-conditional-normalization.v1"

    def __init__(self, logits, legality, *, temperatures=(1., 1., 1., 1.)):
        if len(temperatures) != 4 or any(not math.isfinite(value) or value <= 0 for value in temperatures):
            raise ValueError("four positive finite head temperatures required")
        self.logits = logits
        self.legality = legality
        self.temperatures = tuple(temperatures)

    def conditionals(self, prefix=(), *, raw=False):
        prefix = tuple(prefix)
        mask = self.legality.mask(prefix)
        logits = self.logits(prefix)
        mask = mask.to(logits.device)
        if logits.shape != mask.shape or not bool(torch.isfinite(logits[mask]).all()):
            raise ValueError("conditional logits differ from legal prefix")
        temperature = 1. if raw else self.temperatures[len(prefix)]
        return (logits / temperature).masked_fill(~mask, -torch.inf).log_softmax(-1)

    def prefix_mask(self, prefix=()):
        return self.legality.mask(tuple(prefix)).clone()

    def delay_conditionals_batch(self, card, cells, *, raw=False):
        """[cell,delay] distribution identical to scalar conditional paths, with gradient."""
        if type(card) is not int or not 0 <= card < len(self.legality.card_ids):
            raise ValueError("batched delay card index differs")
        if (not isinstance(cells, torch.Tensor) or cells.dtype != torch.long
                or cells.ndim != 1 or cells.numel() < 1):
            raise ValueError("batched delay cells require nonempty int64 vector")
        indices = cells.to(self.legality.plays.device)
        if (bool((indices < 0).any()) or bool((indices >= self.legality.plays.shape[1]).any())
                or not bool(self.legality.cells[card, indices].all())):
            raise ValueError("batched delay cells have no legal completion")
        batching = getattr(self.logits, "batch_delays", None)
        if batching is None:
            return torch.stack([self.conditionals((1, card, int(cell)), raw=raw)
                for cell in indices.tolist()])
        logits = batching(card, indices)
        mask = self.legality.plays[card, indices].to(logits.device)
        if logits.shape != mask.shape or not bool(torch.isfinite(logits[mask]).all()):
            raise ValueError("batched delay logits differ from legal delay prefixes")
        temperature = 1. if raw else self.temperatures[3]
        return (logits / temperature).masked_fill(~mask, -torch.inf).log_softmax(-1)

    def topk_conditionals(self, prefix, count):
        if type(count) is not int or count < 1:
            raise ValueError("positive top-k required")
        probabilities = self.conditionals(prefix)
        indices = torch.where(torch.isfinite(probabilities))[0].tolist()
        return sorted(indices, key=lambda index: (-float(probabilities[index].detach()), index))[:count]

    def log_prob(self, action):
        return self._log_prob(action, raw=True)

    def behavior_log_prob(self, action):
        return self._log_prob(action, raw=False)

    def _log_prob(self, action, *, raw):
        if not isinstance(action, FullAction) or not self.legality.legal(action):
            raise ValueError("joint log probability requires a legal complete action")
        if action.card_id is None:
            return self.conditionals(raw=raw)[0]
        card = self.legality.card_ids.index(action.card_id)
        return (self.conditionals(raw=raw)[1] + self.conditionals((1,), raw=raw)[card]
                + self.conditionals((1, card), raw=raw)[action.cell]
                + self.conditionals((1, card, action.cell), raw=raw)[action.delay_slot])

    def sample(self, generator, *, greedy=False):
        def select(prefix):
            distribution = self.conditionals(prefix)
            if greedy:
                return int(distribution.argmax())
            return int(torch.multinomial(distribution.exp().detach().cpu(), 1, generator=generator))

        if select(()) == 0:
            action = FullAction()
        else:
            card = select((1,))
            cell = select((1, card))
            delay = select((1, card, cell))
            action = FullAction(self.legality.card_ids[card], cell, delay)
        return action, self.behavior_log_prob(action)
