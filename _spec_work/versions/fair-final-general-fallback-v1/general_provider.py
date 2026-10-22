from copy import deepcopy
import hashlib
from pathlib import Path

from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_final import history_provider, local_reconditioning, refill_local
from native_runner.training.fair_final.history import FairHistory
from native_runner.training.fair_final.search import split_seed


class BoundaryGuard:
    def __init__(self, conditioner):
        self.conditioner = conditioner
        self.unsupported = None

    def __getattr__(self, name):
        return getattr(self.conditioner, name)

    def apply(self, *args, **kwargs):
        self.unsupported = None
        try:
            return self.conditioner.apply(*args, **kwargs)
        except local_reconditioning.UnsupportedLocalBoundary as error:
            self.unsupported = error
            raise


class WorldLease:
    def __init__(self, provider, world, manifest):
        self.provider, self.world = provider, world
        self.manifest = deepcopy(manifest)
        self.commitment = semantic_digest(self.manifest)
        self.closed = False

    def __getattr__(self, name):
        if self.closed:
            raise ValueError("released general belief world")
        return getattr(self.world, name)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.world.close()
        except BaseException:
            self.provider.close()
            raise
        finally:
            if self.provider.active is self:
                self.provider.active = None


class GeneralHistorySearchProvider:
    def __init__(self, factory, *, history, anchor_seed, attempts=4, check_deadline=lambda: None):
        if not isinstance(factory, refill_local.IncrementalRefillBeliefFactory) or factory.closed:
            raise ValueError("general belief requires an open independent incremental refill factory")
        if not isinstance(history, FairHistory):
            raise ValueError("general belief accepts only typed FAIR history")
        if type(attempts) is not int or not 1 <= attempts <= 32:
            raise ValueError("general belief requires a bounded attempt count")
        split_seed(anchor_seed, "validate")
        self.factory, self.history = factory, history
        self.anchor_seed, self.attempts, self.check_deadline = anchor_seed, attempts, check_deadline
        self.verified_history = None
        self.local = self.guard = self.active = None
        self.closed = False
        self.fallback = history_provider.HistorySearchProvider(factory, history=history,
            attempts=attempts, check_deadline=check_deadline)
        modules = (history_provider, local_reconditioning, refill_local)
        sources = {module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in modules}
        sources["general_provider"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.sampler_sha = semantic_digest(dict(schema="fair-final-general-history-provider.v1",
            factory_sha=factory.sampler_sha, sources=sources, attempts=attempts,
            anchor_seed_commitment=semantic_digest([anchor_seed]),
            selection="local-conditioning-unsupported-only-then-independent-history",
            fallback_sampler_sha=self.fallback.sampler_sha, training_admissible=False))

    def _local_provider(self):
        if self.local is None or self.local.closed:
            self.local = refill_local.RefillLocalSearchProvider(self.factory, history=self.history,
                anchor_seed=self.anchor_seed, attempts=self.attempts, check_deadline=self.check_deadline)
            self.guard = BoundaryGuard(self.local.conditioner)
            self.local.conditioner = self.guard
        self.local.history = self.history
        return self.local

    def build(self, information, *, seed):
        if self.closed or self.active is not None:
            raise ValueError("general provider is closed or already leased")
        world = None
        try:
            split_seed(seed, "validate")
            if (not isinstance(self.history, FairHistory)
                    or information.owner != self.history.opening.owner
                    or information.tick != self.history.root_tick
                    or information.sha256 != self.history.root_fair_sha
                    or self.verified_history is not None and not self.history.extends(self.verified_history)):
                raise ValueError("general belief requires an unchanged verified FAIR prefix")
            self.check_deadline()
            local = self._local_provider()
            mode, reason = "local", None
            try:
                world = local.build(information, seed=seed)
                selected = local
            except local_reconditioning.UnsupportedLocalBoundary as error:
                if (error is not self.guard.unsupported or not local.closed
                        or not local.cursor.closed or local.active is not None):
                    raise
                self.check_deadline()
                mode, reason = "independent-history-rebuild", str(error)
                self.fallback.history = self.history
                world = self.fallback.build(information, seed=seed)
                selected = self.fallback
            inner = world.manifest
            if (inner.get("source") != "independent-belief" or inner.get("actual_state_copied") is not False
                    or inner.get("root_fair_sha") != information.sha256
                    or inner.get("sampler_sha") != selected.sampler_sha
                    or inner.get("training_admissible") is not False):
                raise ValueError("general belief construction identity differs")
            manifest = dict(schema="fair-final-general-history-world.v1", source="independent-belief",
                actual_state_copied=False, root_fair_sha=information.sha256,
                history_sha=self.history.sha256, sampler_sha=self.sampler_sha,
                seed_commitment=semantic_digest([seed]), mode=mode, fallback_reason=reason,
                construction=deepcopy(inner), scope="bounded-general-history-engineering",
                training_admissible=False)
            lease = WorldLease(self, world, manifest)
            self.verified_history = self.history
            self.active = lease
            return lease
        except BaseException:
            try:
                if world is not None:
                    world.close()
            finally:
                self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.active is not None:
                self.active.close()
        finally:
            try:
                if self.local is not None:
                    self.local.close()
            finally:
                self.fallback.close()
