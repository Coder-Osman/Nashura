import torch

from .history_belief import IncompatibleHistory
from .policy import FullAction


class ConditionalPlanDistribution:
    def __init__(self, policy, *, elapsed_ticks, check_deadline=lambda: None):
        if type(elapsed_ticks) is not int or elapsed_ticks < 0:
            raise ValueError("history cutoff must be a nonnegative tick interval")
        self.policy, self.check_deadline = policy, check_deadline
        self.conditionals, self.masses = {}, {}
        self.minimum_delay = elapsed_ticks // 2 + 1
        with torch.inference_mode():
            self.log_mass = self._mass(())
        if not bool(torch.isfinite(self.log_mass)):
            raise IncompatibleHistory("frozen opponent has no compatible hidden-plan probability mass")

    def _original(self, prefix):
        self.check_deadline()
        if prefix not in self.conditionals:
            self.conditionals[prefix] = self.policy.conditionals(prefix).detach().double().cpu()
        return self.conditionals[prefix]

    def _mass(self, prefix):
        if prefix in self.masses:
            return self.masses[prefix]
        original = self._original(prefix)
        if len(prefix) == 3:
            mass = torch.logsumexp(original[self.minimum_delay:], dim=0)
        elif self.minimum_delay > 30:
            mass = original[0] if not prefix else original.new_tensor(-torch.inf)
        else:
            children = original.new_full(original.shape, -torch.inf)
            for index in torch.where(torch.isfinite(original))[0].tolist():
                children[index] = 0. if not prefix and index == 0 else self._mass((*prefix, index))
            mass = torch.logsumexp(original + children, dim=0)
        self.masses[prefix] = mass
        return mass

    def conditional(self, prefix=()):
        prefix = tuple(prefix)
        original = self._original(prefix)
        if len(prefix) == 3:
            weights = original.clone()
            weights[:self.minimum_delay] = -torch.inf
        else:
            children = original.new_full(original.shape, -torch.inf)
            for index in torch.where(torch.isfinite(original))[0].tolist():
                children[index] = 0. if not prefix and index == 0 else self._mass((*prefix, index))
            weights = original + children
        if not bool(torch.isfinite(weights).any()):
            raise IncompatibleHistory("hidden-plan prefix has no compatible completion")
        return weights - torch.logsumexp(weights, dim=0)

    def sample(self, generator):
        def select(prefix):
            self.check_deadline()
            return int(torch.multinomial(self.conditional(prefix).exp(), 1, generator=generator))

        if select(()) == 0:
            return FullAction()
        card = select((1,))
        cell = select((1, card))
        delay = select((1, card, cell))
        return FullAction(self.policy.legality.card_ids[card], cell, delay)


class HistoryConditionedOpponent:
    def __init__(self, opponent, history):
        self.opponent, self.history = opponent, history
        self.profile = dict(calls=0, minimum_log_mass=None, maximum_prefixes=0)

    def sample(self, information, generator):
        if (information.owner != 1-self.history.opening.owner
                or not self.history.opening.tick <= information.tick < self.history.root_tick):
            raise ValueError("conditioned opponent requires its bound historical interval and player")
        first_tick = next((event.tick for event in self.history.deployments
            if event.tick > information.tick), self.history.root_tick + 1)
        cutoff = min(first_tick, self.history.root_tick)
        with torch.inference_mode():
            policy, _ = self.opponent.predict(information)
            distribution = ConditionalPlanDistribution(policy, elapsed_ticks=cutoff-information.tick)
            action = distribution.sample(generator)
        self.profile["calls"] += 1
        previous = self.profile["minimum_log_mass"]
        self.profile["minimum_log_mass"] = min(float(distribution.log_mass),
            float(distribution.log_mass) if previous is None else previous)
        self.profile["maximum_prefixes"] = max(self.profile["maximum_prefixes"], len(distribution.conditionals))
        return action
