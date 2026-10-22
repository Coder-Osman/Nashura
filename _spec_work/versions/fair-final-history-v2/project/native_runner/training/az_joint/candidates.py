import math

import torch

from .codec import JointActionCodec


def candidate_prior(probabilities, *, seed):
    probabilities = probabilities.detach().to(device="cpu", dtype=torch.float64)
    if (probabilities.shape != (JointActionCodec.size,) or not bool(torch.isfinite(probabilities).all())
            or bool((probabilities < 0).any()) or abs(float(probabilities.sum()) - 1) > 1e-5):
        raise ValueError("invalid full joint distribution")
    active = torch.nonzero(probabilities[1:] > 0).flatten() + 1
    wait = float(probabilities[0])
    if not len(active):
        return {"wait": 1.}
    mass = float(probabilities[active].sum())
    conditional = probabilities[active] / mass
    if len(active) <= 31:
        return {"wait": wait, **{JointActionCodec.to_key(int(action)): float(probabilities[action])
                               for action in active}}
    proposal = .8 * conditional + .2 / len(active)
    generator = torch.Generator().manual_seed(seed)
    samples = torch.multinomial(proposal, 31, replacement=True, generator=generator)
    indices, counts = samples.unique(return_counts=True)
    weights = counts * conditional[indices] / proposal[indices]
    weights = weights / weights.sum() * mass
    prior = {"wait": wait}
    for index, weight in zip(indices, weights):
        prior[JointActionCodec.to_key(int(active[index]))] = float(weight)
    if not math.isclose(sum(prior.values()), 1., abs_tol=1e-5):
        raise ValueError("candidate importance correction lost probability mass")
    return prior


def sample_visits(visits, *, seed):
    if sum(visits.values()) != 128 or any(type(count) is not int or count < 0 for count in visits.values()):
        raise ValueError("execution requires a complete 128-visit root")
    actions = sorted(visits)
    generator = torch.Generator().manual_seed(seed)
    choice = int(torch.multinomial(torch.tensor([visits[action] for action in actions], dtype=torch.float64),
                                   1, generator=generator))
    return actions[choice]
