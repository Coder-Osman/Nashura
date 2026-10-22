from dataclasses import dataclass
import math
import random

import torch

from .policy import FullAction


@dataclass(frozen=True)
class Candidate:
    action: FullAction
    proposal: str
    raw_logp: float
    prior: float
    card_mass: float


def structured_candidates(policy, *, seed, maximum=32):
    if policy.temperatures != (1., 1., 1., 1.):
        raise ValueError("search projection requires the untempered policy")
    if type(maximum) is not int or not 1 <= maximum <= 32:
        raise ValueError("candidate maximum must be in 1..32")
    if not bool(policy.prefix_mask()[0]):
        raise ValueError("search requires WAIT")
    legal_cards = torch.where(policy.prefix_mask((1,)))[0].tolist()
    capacity = max(maximum, 1 + len(legal_cards))
    if capacity > 32:
        raise ValueError("legal card coverage exceeds search capacity")
    selected = {FullAction().key: (FullAction(), "WAIT")}

    def add(action, proposal):
        if len(selected) < capacity and action.key not in selected:
            if not policy.legality.legal(action):
                raise ValueError("candidate generator produced an illegal action")
            selected[action.key] = action, proposal

    for card in legal_cards:
        cell = policy.topk_conditionals((1, card), 1)[0]
        delay = policy.topk_conditionals((1, card, cell), 1)[0]
        add(FullAction(policy.legality.card_ids[card], cell, delay), "CARD_ELITE")
    for rank in (0, 1):
        for card in legal_cards:
            cells = policy.topk_conditionals((1, card), 2)
            if rank >= len(cells):
                continue
            cell = cells[rank]
            delay = policy.topk_conditionals((1, card, cell), 1)[0]
            add(FullAction(policy.legality.card_ids[card], cell, delay), "SPATIAL_PEAK")
            if len(selected) >= max(1 + len(legal_cards), capacity * 3 // 4):
                continue
            for neighbor in (delay - 1, delay + 1):
                if 1 <= neighbor <= 30 and bool(policy.prefix_mask((1, card, cell))[neighbor]):
                    add(FullAction(policy.legality.card_ids[card], cell, neighbor), "TIME_NEIGHBOR")
    generator = torch.Generator().manual_seed(seed)
    for ordinal in range(capacity * 8):
        if len(selected) >= capacity or not legal_cards:
            break
        card = legal_cards[int(torch.randint(len(legal_cards), (), generator=generator))]
        cell = int(torch.multinomial(policy.conditionals((1, card)).exp().detach().cpu(), 1, generator=generator))
        delay = int(torch.multinomial(policy.conditionals((1, card, cell)).exp().detach().cpu(), 1, generator=generator))
        add(FullAction(policy.legality.card_ids[card], cell, delay), "RANDOM")
    modes = policy.conditionals().detach().exp().cpu()
    cards = policy.conditionals((1,)).detach().exp().cpu() if legal_cards else None
    raw = {key: float(policy.log_prob(action).detach()) for key, (action, _) in selected.items()}
    result = []
    for key, (action, proposal) in selected.items():
        if action.card_id is None:
            mass = probability = float(modes[0])
        else:
            card = policy.legality.card_ids.index(action.card_id)
            mass = float(modes[1] * cards[card])
            peers = [raw[peer] for peer, (other, _) in selected.items() if other.card_id == action.card_id]
            maximum_logp = max(peers)
            probability = mass * math.exp(raw[key] - maximum_logp) / sum(math.exp(value - maximum_logp) for value in peers)
        result.append(Candidate(action, proposal, raw[key], probability, mass))
    if not math.isclose(sum(entry.prior for entry in result), 1., abs_tol=1e-5):
        raise ValueError("candidate projection lost mode/card mass")
    return tuple(result)


def act_only_noise(candidates, *, seed, fraction=.25, concentration=10.):
    if not 0 <= fraction <= 1 or not math.isfinite(concentration) or concentration <= 0:
        raise ValueError("invalid ACT-only root noise")
    priors = {entry.action.key: entry.prior for entry in candidates}
    actions = [entry.action.key for entry in candidates if entry.action.card_id is not None]
    if not actions or fraction == 0:
        return priors
    rng = random.Random(seed)
    draws = [rng.gammavariate(concentration / len(actions), 1.) for action in actions]
    total = sum(draws)
    mass = sum(priors[action] for action in actions)
    if total <= 0:
        raise ValueError("Dirichlet sample has zero mass")
    for action, draw in zip(actions, draws):
        priors[action] = (1 - fraction) * priors[action] + fraction * mass * draw / total
    return priors
