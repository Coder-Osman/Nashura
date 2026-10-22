from copy import deepcopy

import torch

from ..fair_search.contracts import require_sha, semantic_digest
from .adapter import source_policy
from .episode import terminal_result
from .search import Transition
from .visibility import validate_fair_view


class FrozenSearchInference:
    def __init__(self, model, *, checkpoint_sha, controller_sha, source_adapter=False):
        self.model = model
        self.checkpoint_sha = require_sha(checkpoint_sha)
        self.controller_sha = require_sha(controller_sha)
        self.source_adapter = source_adapter
        if any(module.training for module in model.modules()):
            raise ValueError("search requires a frozen eval-mode model")

    def predict(self, information):
        if any(module.training for module in self.model.modules()):
            raise ValueError("frozen search model changed training mode")
        with torch.inference_mode():
            if self.source_adapter:
                return source_policy(self.model, information.frame, information.legality)
            policies, logits = self.model(information.frame, [information.legality])
            return policies[0], logits

    def sample(self, information, generator):
        with torch.inference_mode():
            policy, _ = self.predict(information)
            return policy.sample(generator)[0]


class RolloutSearchWorld:
    def __init__(self, rollout, *, owner, sidecar_factory, manifest):
        if type(owner) is not int or owner not in (0, 1) or not rollout.runtime.clock.boundary(owner):
            raise ValueError("belief rollout requires a root-owner decision boundary")
        if (manifest.get("source") != "independent-belief" or manifest.get("actual_state_copied") is not False
                or manifest.get("root_fair_sha") != rollout.freeze_due_information()[owner].sha256):
            raise ValueError("independent belief manifest required; no actual-state fallback")
        self.rollout, self.owner, self.sidecar_factory = rollout, owner, sidecar_factory
        self.manifest = deepcopy(manifest)
        self.commitment = semantic_digest(manifest)
        self.root = rollout.snapshot(sidecar_factory)
        self.closed = False

    def root_information(self):
        return self.root_owner_information()

    def root_owner_information(self):
        information = self.rollout.freeze_due_information()
        if self.owner not in information or not self.rollout.runtime.clock.boundary(self.owner):
            raise ValueError("value requested outside the root-owner decision boundary")
        return information[self.owner]

    def restore_root(self):
        if self.closed:
            raise ValueError("released belief world")
        self.root.restore()

    def _commit(self, opponent, generator, action=None):
        information = self.rollout.freeze_due_information()
        runtime = self.rollout.runtime
        selected = {}
        for owner, info in information.items():
            validate_fair_view(runtime.frozen[owner], owner)
            if owner == self.owner:
                if action is None:
                    raise ValueError("root action missing at own decision boundary")
                selected[owner] = action
            else:
                selected[owner] = opponent.sample(info, generator)
        if action is not None and self.owner not in selected:
            raise ValueError("own action supplied outside its decision boundary")
        if not selected:
            raise ValueError("empty decision group")
        self.rollout.commit_simultaneous(selected)

    def transition(self, action, *, opponent, generator, hard_tick, maximum_decisions, check_deadline):
        if self.closed:
            raise ValueError("released belief world")
        runtime = self.rollout.runtime
        start = runtime.clock.tick
        if not runtime.clock.boundary(self.owner):
            raise ValueError("edge must start at a completed own decision boundary")
        with self.rollout.snapshot(self.sidecar_factory) as previous:
            if hard_tick <= start or maximum_decisions < 1:
                return Transition(False, 0, 0)
            check_deadline()
            self._commit(opponent, generator, action)
            reservation = runtime.clock.pending[self.owner].identity
            decisions = 1
            while True:
                check_deadline()
                if runtime.clock.tick >= hard_tick:
                    elapsed = runtime.clock.tick - start
                    previous.restore()
                    return Transition(False, decisions, elapsed)
                self.rollout.advance()
                if runtime.failed or runtime.truncated:
                    raise ValueError("failed or truncated belief transition")
                outcome = next((entry for entry in reversed(runtime.clock.evidence)
                    if entry["reservation"] == reservation), None)
                if runtime.ended:
                    raw = runtime.environment.raw_observation
                    result = terminal_result(runtime.environment.observe_dual(policy_features_only=True)[self.owner].terminal,
                        raw, truncated=False, failed=False)
                    if outcome is None:
                        raise ValueError("terminal edge has no reservation settlement")
                    value = 0. if result["winner"] is None else (1. if result["winner"] == self.owner else -1.)
                    return Transition(True, decisions, runtime.clock.tick-start, terminal_value=value, reason=outcome["reason"])
                if runtime.clock.boundary(self.owner):
                    if outcome is None:
                        raise ValueError("own wake precedes edge settlement")
                    return Transition(True, decisions, runtime.clock.tick-start,
                        information=self.root_owner_information(), reason=outcome["reason"])
                if runtime.clock.ready:
                    if decisions >= maximum_decisions:
                        elapsed = runtime.clock.tick - start
                        previous.restore()
                        return Transition(False, decisions, elapsed)
                    self._commit(opponent, generator)
                    decisions += 1

    def close(self):
        if self.closed:
            return
        try:
            self.root.restore()
        finally:
            self.root.close()
            self.closed = True
