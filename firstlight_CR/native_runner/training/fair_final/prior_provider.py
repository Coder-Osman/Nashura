from copy import deepcopy

from ..fair_search.contracts import semantic_digest
from .history_provider import HistorySearchProvider, RecoveringHistoryCursor
from .prior_history import PriorHistoryBeliefFactory
from .search_runtime import RolloutSearchWorld


class PriorHistorySearchProvider(HistorySearchProvider):
    def __init__(self, factory, *, history, attempts=4, check_deadline=lambda: None):
        if not isinstance(factory, PriorHistoryBeliefFactory):
            raise ValueError("prior search requires a verified prior-history factory")
        super().__init__(factory, history=history, attempts=attempts, check_deadline=check_deadline)
        self.sampler_sha = semantic_digest(dict(schema="fair-final-prior-history-search.v1",
            history_sampler_sha=factory.sampler_sha, prior_sha=factory.prior.sha256,
            attempts=attempts, root_sampling="fresh-independent-world-per-build",
            reuse="within-root-only", conditioning="verified-native-prior-history"))

    def build(self, information, *, seed):
        if self.closed or self.active is not None:
            raise ValueError("prior search provider is closed or already leased")
        cursor = RecoveringHistoryCursor(self.factory, seed=seed, attempts=self.attempts,
            check_deadline=self.check_deadline)
        world = None
        try:
            session = cursor.sync(self.history, information)
            conditioning = deepcopy(session.conditioning_manifest)
            expected = dict(source="independent-belief", actual_state_copied=False,
                root_fair_sha=information.sha256, history_sha=self.history.sha256,
                owner=information.owner, root_tick=information.tick, native_draw_verified=True,
                sampler_sha=self.factory.sampler_sha, prior_sha=self.factory.prior.sha256,
                opponent_controller_sha=self.factory.opponent.controller_sha,
                opponent_checkpoint_sha=self.factory.opponent.checkpoint_sha,
                scope="native-fixed-hog-prior-integration", training_admissible=False)
            if any(conditioning.get(key) != value or type(conditioning.get(key)) is not type(value)
                    for key, value in expected.items()):
                raise ValueError("prior search conditioning identity differs")
            manifest = dict(source="independent-belief", actual_state_copied=False,
                root_fair_sha=information.sha256, history_sha=self.history.sha256,
                sampler_sha=self.sampler_sha, prior_sha=self.factory.prior.sha256,
                seed_commitment=semantic_digest([seed]), conditioning=conditioning,
                opponent_controller_sha=self.factory.opponent.controller_sha,
                opponent_checkpoint_sha=self.factory.opponent.checkpoint_sha,
                root_sampling="fresh-independent-world-per-build",
                scope="fixed-hog-prior-search-engineering", training_admissible=False)
            world = RolloutSearchWorld(session.rollout, owner=information.owner,
                sidecar_factory=self.factory.sidecar_factory, manifest=manifest)
            original_close = world.close

            def close():
                try:
                    original_close()
                finally:
                    try:
                        cursor.close()
                    finally:
                        if self.active is world:
                            self.active = None

            world.close = close
            self.active = world
            return world
        except BaseException:
            try:
                if world is not None:
                    world.close()
            finally:
                cursor.close()
            raise
