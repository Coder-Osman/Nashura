"""SSPI adapters for the existing completed-boundary PUCT implementation."""

import torch

from ..fair_final.policy import FullAction
from ..fair_final.search import SearchLimits
from ..fair_final.search_runtime import FrozenSearchInference
from .protocol import ActionMode, DELAY_SLOTS, SSPIAction, from_full_action, to_full_action
from .model import VALUE_ORDER


def budget_search_sha(limits: SearchLimits, simulations: int) -> str:
    if type(simulations) is not int or simulations != 128:
        raise ValueError("SSPI pilot search budget is fixed at 128")
    from ..fair_search.contracts import semantic_digest

    return semantic_digest(dict(schema="fair-final-explicit-budget-search.v1",
        base_limits_sha=limits.sha256, valid_simulations=simulations))


class SSPIJointPolicy:
    """Expose tagged SSPI probabilities through the legacy native PUCT action ABI."""

    temperatures = (1., 1., 1., 1.)

    def __init__(self, policy):
        self.policy = policy
        self.legality = policy.legality

    @staticmethod
    def _prefix(prefix):
        prefix = tuple(prefix)
        if not prefix:
            return ()
        if prefix == (1,):
            return (ActionMode.PLAY,)
        if len(prefix) == 2 and prefix[0] == 1:
            return (ActionMode.PLAY, int(prefix[1]))
        if len(prefix) == 3 and prefix[0] == 1:
            return (ActionMode.PLAY, int(prefix[1]), int(prefix[2]))
        raise ValueError("legacy PUCT prefix does not map to an SSPI action branch")

    def conditionals(self, prefix=()):
        prefix = tuple(prefix)
        values = self.policy.conditionals(self._prefix(prefix))
        if not prefix:
            return values[:2]
        if len(prefix) != 3:
            return values
        result = values.new_full((31,), -torch.inf)
        return result.index_copy(0, torch.tensor(DELAY_SLOTS, device=values.device), values)

    def prefix_mask(self, prefix=()):
        prefix = tuple(prefix)
        mask = self.policy.legality.mask(self._prefix(prefix))
        if not prefix:
            return mask[:2].clone()
        if len(prefix) != 3:
            return mask.clone()
        result = torch.zeros(31, dtype=torch.bool, device=mask.device)
        return result.index_copy(0, torch.tensor(DELAY_SLOTS, device=mask.device), mask)

    def topk_conditionals(self, prefix, count):
        if type(count) is not int or count < 1:
            raise ValueError("positive top-k required")
        probabilities = self.conditionals(prefix)
        indices = torch.where(torch.isfinite(probabilities))[0]
        if indices.numel() == 0:
            return []
        # Preserve the old order exactly: descending score, ascending index
        # for ties. One stable tensor sort avoids per-cell Python float reads.
        ranking = torch.argsort(probabilities.index_select(0, indices),
            descending=True, stable=True)
        return indices.index_select(0, ranking[:count]).tolist()

    def log_prob(self, action: FullAction):
        if not isinstance(action, FullAction):
            raise TypeError("PUCT adapter requires a native complete action")
        return self.policy.log_prob(from_full_action(action))

    def sample(self, generator, *, greedy=False):
        action, log_probability = self.policy.sample(generator, greedy=greedy)
        return to_full_action(action), log_probability


def make_search_inference(model, *, checkpoint_sha, controller_sha):
    model.eval()
    return FrozenSearchInference(model, checkpoint_sha=checkpoint_sha,
        controller_sha=controller_sha, policy_adapter=SSPIJointPolicy, value_order=VALUE_ORDER)


class SSPIHistorySearchProvider:
    """Reuse one owner's independently reconstructed FAIR history between search roots."""

    def __init__(self, factory, *, history, seed, attempts=4, check_deadline=lambda: None):
        from ..fair_final.history_provider import RecoveringHistoryCursor
        from ..fair_search.contracts import semantic_digest

        if type(attempts) is not int or not 1 <= attempts <= 32:
            raise ValueError("SSPI history search requires bounded reconstruction attempts")
        self.factory, self.history = factory, history
        self.cursor = RecoveringHistoryCursor(factory, seed=seed, attempts=attempts,
            check_deadline=check_deadline)
        self.check_deadline = check_deadline
        self.active = None
        self.closed = False
        self.sampler_sha = semantic_digest(dict(schema="sspi-history-search-provider.v1",
            history_sampler_sha=factory.sampler_sha, attempts=attempts,
            root_sampling="fresh-independent-world-per-root",
            replay_session="owner-local-reusable-fair-prefix"))

    def update_history(self, history):
        if self.closed or not history.extends(self.history):
            raise ValueError("SSPI belief history must extend the frozen public prefix")
        self.history = history

    def build(self, information, *, seed):
        from ..fair_final.search_runtime import RolloutSearchWorld
        from ..fair_search.contracts import semantic_digest

        if self.closed or self.active is not None:
            raise ValueError("SSPI history provider is closed or already has a leased root")
        self.check_deadline()
        session = self.cursor.sync(self.history, information)
        manifest = dict(source="independent-belief", actual_state_copied=False,
            root_fair_sha=information.sha256, history_sha=self.history.sha256,
            sampler_sha=self.sampler_sha, seed_commitment=semantic_digest([seed]),
            opponent_controller_sha=self.factory.opponent.controller_sha,
            opponent_checkpoint_sha=self.factory.opponent.checkpoint_sha,
            root_sampling="fresh-independent-world-per-root",
            scope="sspi-fixed-hog-history-search", training_admissible=False)
        world = RolloutSearchWorld(session.rollout, owner=information.owner,
            sidecar_factory=self.factory.sidecar_factory, manifest=manifest)
        self.active = world
        close_world = world.close

        def release_root():
            try:
                close_world()
            finally:
                if self.active is world:
                    self.active = None

        world.close = release_root
        return world

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.active is not None:
                self.active.close()
        finally:
            self.cursor.close()


def native_action(action: SSPIAction) -> FullAction:
    return to_full_action(action)
