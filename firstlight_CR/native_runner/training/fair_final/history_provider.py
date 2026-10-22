import time

from ..fair_search.contracts import semantic_digest
from .history import FairHistory
from .history_belief import IncompatibleHistory
from .search import split_seed
from .search_runtime import RolloutSearchWorld


class RecoveringHistoryCursor:
    def __init__(self, factory, *, seed, attempts=4, check_deadline=lambda: None):
        if type(attempts) is not int or not 1 <= attempts <= 32:
            raise ValueError("history recovery requires a bounded attempt count")
        split_seed(seed, "validate")
        self.factory, self.seed, self.attempts = factory, seed, attempts
        self.check_deadline = check_deadline
        self.session = self.history = None
        self.closed = False
        self.events = []

    def _release(self):
        session, self.session = self.session, None
        if session is not None:
            try:
                session.close()
            finally:
                session.rollout.runtime.environment.native.close_transport()

    def sync(self, history, information):
        if (self.closed or not isinstance(history, FairHistory)
                or information.owner != history.opening.owner
                or information.tick != history.root_tick
                or information.sha256 != history.root_fair_sha
                or self.history is not None and not history.extends(self.history)):
            raise ValueError("history cursor requires an unchanged verified FAIR prefix")
        self.check_deadline()
        started = time.monotonic()
        previous_tick = self.history.root_tick if self.history is not None else history.opening.tick
        mode, reason = "cold", None
        if self.session is not None:
            mode = "incremental"
            try:
                self.session.sync(history, information, check_deadline=self.check_deadline)
            except IncompatibleHistory as error:
                mode, reason = "rebuild", str(error)
                self._release()
        try:
            if self.session is None:
                self.session = self.factory.build(history, information,
                    seed=split_seed(self.seed, "conditional-history", history.sha256),
                    attempts=self.attempts, check_deadline=self.check_deadline)
            self.history = history
        except BaseException:
            self.events.append(dict(mode=mode, status="failed", from_tick=previous_tick,
                to_tick=history.root_tick, reason=reason, seconds=time.monotonic()-started))
            raise
        self.events.append(dict(mode=mode, status="verified", from_tick=previous_tick,
            to_tick=history.root_tick, reason=reason, seconds=time.monotonic()-started))
        return self.session

    def close(self):
        if not self.closed:
            self.closed = True
            self._release()


class HistorySearchProvider:
    def __init__(self, factory, *, history, attempts=4, check_deadline=lambda: None):
        if not isinstance(history, FairHistory):
            raise ValueError("search provider accepts only typed FAIR history")
        if type(attempts) is not int or not 1 <= attempts <= 32:
            raise ValueError("search belief requires a bounded attempt count")
        self.factory, self.history = factory, history
        self.attempts, self.check_deadline = attempts, check_deadline
        self.active = None
        self.closed = False
        self.sampler_sha = semantic_digest(dict(schema="fair-final-history-search-engineering.v1",
            history_sampler_sha=factory.sampler_sha, attempts=attempts,
            root_sampling="fresh-independent-world-per-build",
            reuse="within-root-only"))

    def build(self, information, *, seed):
        if self.closed or self.active is not None:
            raise ValueError("history search provider is closed or already leased")
        cursor = RecoveringHistoryCursor(self.factory, seed=seed, attempts=self.attempts,
            check_deadline=self.check_deadline)
        world = None
        try:
            session = cursor.sync(self.history, information)
            manifest = dict(source="independent-belief", actual_state_copied=False,
                root_fair_sha=information.sha256, history_sha=self.history.sha256,
                sampler_sha=self.sampler_sha, seed_commitment=semantic_digest([seed]),
                opponent_controller_sha=self.factory.opponent.controller_sha,
                opponent_checkpoint_sha=self.factory.opponent.checkpoint_sha,
                root_sampling="fresh-independent-world-per-build",
                scope="fixed-hog-history-search-engineering", training_admissible=False)
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

    def close(self):
        if not self.closed:
            self.closed = True
            if self.active is not None:
                self.active.close()
