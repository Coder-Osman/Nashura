import random

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from ..fair_search.belief import NativeConditioner, OwnInitialDraw, initial_draw
from ..fair_search.config import HOG_DECK
from ..fair_search.contracts import semantic_digest
from .deck_belief import FrozenDeckPrior, PublicCycleBelief, PublicPlay
from .history import FairHistory
from .history_belief import FULL_POLICY_FRACTION, HIDDEN_PLAN_PROPOSAL, HistoryBeliefFactory, HistoryReplaySession
from .native import FinalBattleEnv
from .rollout import FinalRollout
from .search import split_seed


class HistoryCycleCursor:
    def __init__(self, prior, *, opening):
        initial = FairHistory(opening, (), (), opening.tick, opening.root_fair_sha)
        self.cycle = PublicCycleBelief(prior, owner=1-opening.owner, opening_tick=opening.tick)
        self.history = initial
        self.last_delta = dict(added_plays=0, order_checks=0)

    def sync(self, history):
        if not isinstance(history, FairHistory) or not history.extends(self.history):
            raise ValueError("prior cursor requires the same typed FAIR history prefix")
        delta = tuple(PublicPlay(entry.tick, entry.card_id)
            for entry in history.deployments[len(self.history.deployments):]
            if entry.owner == self.cycle.owner)
        result = self.cycle.advance(delta, from_tick=self.history.root_tick,
            to_tick=history.root_tick, expected_evidence_sha=self.cycle.evidence_sha)
        self.history, self.last_delta = history, result
        return result

    def sample(self, history, *, seed):
        self.sync(history)
        return self.cycle.sample(seed=seed)


class PriorHistoryBeliefFactory(HistoryBeliefFactory):
    def __init__(self, native_factory, *, prior, catalog, opponent, sidecar_factory):
        if (not isinstance(prior, FrozenDeckPrior) or len(prior.entries) != 1
                or prior.entries[0].cards != tuple(sorted(HOG_DECK))):
            raise ValueError("native prior integration currently supports only the verified base Hog deck")
        super().__init__(native_factory, catalog=catalog, opponent=opponent, sidecar_factory=sidecar_factory)
        self.prior, self.cursors, self.closed = prior, {}, False
        self.sampler_sha = semantic_digest(dict(schema="fair-final-native-prior-history.v2",
            history_schema=FairHistory.schema, rejection="own-visible-attestation-opponent-model-generated.v1",
            prior_sha=prior.sha256, cycle_schema="fair-final-public-cycle-sampler.v1",
            rules=prior.to_payload()["rules"], execution="legal-reservations-only",
            hidden_plan=HIDDEN_PLAN_PROPOSAL, full_policy_fraction=FULL_POLICY_FRACTION,
            root_sampling="fresh-draw-independent-engine-per-build",
            reuse="public-cycle-support-only", maximum_rebuild_attempts=32,
            posterior="approximate-proposal-not-exact-bayes",
            root_draw_validation="unordered-logical-hand-exact-fifo-native-slots-authoritative.v1"))

    def create(self, history, *, seed):
        if self.closed or not isinstance(history, FairHistory):
            raise ValueError("prior factory requires open typed FAIR history")
        split_seed(seed, "validate")
        owner = history.opening.owner
        cursor = self.cursors.get(owner)
        if cursor is None:
            cursor = HistoryCycleCursor(self.prior, opening=history.opening)
        completion = cursor.sample(history, seed=split_seed(seed, "opponent-draw"))
        self.cursors[owner] = cursor
        native = self.native_factory()
        try:
            engine_seed = split_seed(seed, "belief-engine") % (2**31-1)

            def match(deck0, deck1, match_seed):
                return MatchConfig(deck0=deck0, deck1=deck1, seed=match_seed,
                    end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs())

            decks, observed = NativeConditioner(native, match).create(
                OwnInitialDraw(owner, history.opening.own_draw), seed=engine_seed,
                rng=random.Random(split_seed(seed, "own-draw")))
            opponent = 1-owner
            _, slots = initial_draw(next(player for player in observed["players"] if player["owner"] == opponent))
            ordered = [None] * 8
            for slot, card in zip(slots, completion.initial_draw):
                ordered[slot] = card
            decks = list(decks)
            decks[opponent] = tuple(ordered)
            environment = FinalBattleEnv(native=native)
            environment.reset(match_config=match(*decks, engine_seed),
                options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                    allow_initial_remaining_runtime_rejections=True))
            actual_draw = initial_draw(next(player for player in environment.raw_observation["players"]
                if player["owner"] == opponent))[0]
            if actual_draw != completion.initial_draw:
                raise ValueError("sampled prior draw changed during native conditional initialization")
            native.start_atomic_steps()
            rollout = FinalRollout(environment, catalog=self.catalog, execution_seed=split_seed(seed, "rollout"))
            session = HistoryReplaySession(rollout, opening=history.opening, opponent=self.opponent,
                seed=split_seed(seed, "opponent-history"), sidecar_factory=self.sidecar_factory)
            session.draw_completion = completion
            session.conditioning_manifest = dict(source="independent-belief", actual_state_copied=False,
                root_fair_sha=history.root_fair_sha, history_sha=history.sha256, owner=owner,
                root_tick=history.root_tick, sampler_sha=self.sampler_sha, prior_sha=self.prior.sha256,
                completion_manifest=completion.manifest, seed_commitment=semantic_digest([seed]),
                opponent_controller_sha=self.opponent.controller_sha,
                opponent_checkpoint_sha=self.opponent.checkpoint_sha,
                scope="native-fixed-hog-prior-integration", training_admissible=False)
            return session
        except BaseException:
            native.close_transport()
            raise

    def build(self, history, information, *, seed, attempts=4, check_deadline=lambda: None):
        if (self.closed or not isinstance(history, FairHistory)
                or information.owner != history.opening.owner or information.tick != history.root_tick
                or information.sha256 != history.root_fair_sha):
            raise ValueError("native prior root differs from typed FAIR history")
        session = super().build(history, information, seed=seed, attempts=attempts, check_deadline=check_deadline)
        try:
            raw = session.rollout.runtime.environment.native.observe()
            current = initial_draw(next(player for player in raw["players"]
                if player["owner"] == 1-history.opening.owner))[0]
            expected = session.draw_completion.current_draw
            if set(current[:4]) != set(expected[:4]) or current[4:] != expected[4:]:
                raise ValueError("native reconstructed hand and cycle differ from sampled public completion: "
                    f"tick={raw['tick']} expected_tick={history.root_tick} initial={session.draw_completion.initial_draw} "
                    f"expected={expected} observed={current}")
            session.conditioning_manifest["native_draw_verified"] = True
            session.conditioning_manifest["native_current_draw"] = list(current)
            session.conditioning_manifest["logical_hand_order_differs"] = current[:4] != expected[:4]
            return session
        except BaseException:
            try:
                session.close()
            finally:
                session.rollout.runtime.environment.native.close_transport()
            raise

    def close(self):
        self.closed = True
        self.cursors.clear()
