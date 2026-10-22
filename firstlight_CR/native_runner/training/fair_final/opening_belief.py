from dataclasses import dataclass
import random

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from ..fair_search.belief import NativeConditioner, OwnInitialDraw
from ..fair_search.config import HOG_DECK
from ..fair_search.contracts import semantic_digest
from .native import FinalBattleEnv
from .rollout import FinalRollout
from .search import split_seed
from .search_runtime import RolloutSearchWorld
from .visibility import validate_fair_view


@dataclass(frozen=True)
class OpeningEvidence:
    root_fair_sha: str
    owner: int
    tick: int
    own_draw: tuple

    @classmethod
    def from_view(cls, observation, information, *, opening_tick):
        validate_fair_view(observation, information.owner)
        if (observation.tick != opening_tick or information.tick != opening_tick
                or observation.events or observation.pending_actions):
            raise ValueError("opening-only belief rejects historical or pending roots")
        own = next(player for player in observation.players if player.owner == information.owner)
        slots = own.metadata["hand_slot_by_card"]
        hand = tuple(sorted(own.hand, key=lambda card: int(slots.get(str(card), slots.get(card)))))
        cycle = tuple(own.cycle)
        draw = (*hand, *cycle)
        if (len(hand) != 4 or len(cycle) != 4 or len(set(draw)) != 8 or set(draw) != set(HOG_DECK)
                or own.next_card != cycle[0]):
            raise ValueError("opening own visible draw is incomplete")
        return cls(information.sha256, information.owner, information.tick, draw)


class OpeningBeliefProvider:
    sampler_sha = semantic_digest(dict(schema="fair-final-opening-belief-acceptance.v1",
        scope="no-action-opening-only", deck_prior=list(HOG_DECK),
        prior_source="fixed-hog-experiment-not-general-empirical-prior", conditioning="own-visible-draw"))

    def __init__(self, native_factory, *, evidence, catalog, sidecar_factory):
        self.native_factory, self.evidence = native_factory, evidence
        self.catalog, self.sidecar_factory = catalog, sidecar_factory

    def build(self, information, *, seed):
        evidence = self.evidence
        if (information.sha256 != evidence.root_fair_sha or information.owner != evidence.owner
                or information.tick != evidence.tick):
            raise ValueError("opening belief information differs from captured own evidence")
        native = self.native_factory()
        world = None
        try:
            engine_seed = split_seed(seed, "belief-engine") % (2**31-1)

            def match(deck0, deck1, match_seed):
                return MatchConfig(deck0=deck0, deck1=deck1, seed=match_seed,
                    end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs())

            conditioner = NativeConditioner(native, match)
            decks, _ = conditioner.create(OwnInitialDraw(evidence.owner, evidence.own_draw),
                seed=engine_seed, rng=random.Random(split_seed(seed, "draw")))
            environment = FinalBattleEnv(native=native)
            environment.reset(match_config=match(*decks, engine_seed),
                options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                    allow_initial_remaining_runtime_rejections=True))
            native.start_atomic_steps()
            rollout = FinalRollout(environment, catalog=self.catalog, execution_seed=split_seed(seed, "rollout"))
            root = rollout.freeze_due_information()[evidence.owner]
            if root.sha256 != information.sha256:
                raise ValueError("independently initialized belief changed the root FAIR state")
            manifest = dict(source="independent-belief", actual_state_copied=False,
                root_fair_sha=root.sha256, sampler_sha=self.sampler_sha,
                seed_commitment=semantic_digest([seed, engine_seed]),
                draw_commitment=semantic_digest([evidence.own_draw, decks]),
                scope="opening-only-engineering-acceptance", training_admissible=False)
            world = RolloutSearchWorld(rollout, owner=evidence.owner, sidecar_factory=self.sidecar_factory, manifest=manifest)
            original_close = world.close

            def close():
                try:
                    original_close()
                finally:
                    native.close_transport()

            world.close = close
            return world
        except BaseException:
            if world is not None:
                world.close()
            else:
                native.close_transport()
            raise
