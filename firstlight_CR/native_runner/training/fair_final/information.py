from native_runner.contracts import ObservationTier
from ..tracking import DeterministicPublicTracker, PublicCardState, AvailabilityIndex
from ..v4.factory import production_semantic_bundle
from ..v4.tensorizer import UniversalObservationTensorizerV4
from .visibility import event_visible_to_owner, validate_fair_view


class FairPublicTracker(DeterministicPublicTracker):
    def __init__(self, *, owner, own_deck, card_costs):
        if type(owner) is not int or owner not in (0, 1):
            raise ValueError("FAIR tracker requires one actor owner")
        super().__init__(decks={0: own_deck, 1: own_deck}, card_costs=card_costs,
            ability_cost_by_owner={0: 0., 1: 0.}, ability_cooldown_ms_by_owner={0: 50, 1: 50},
            evolution_cycle_required={})
        self.actor_owner = owner
        self.decks[1 - owner] = ()

    def reset(self, *, tick, initial_elixir):
        self.decks[1 - self.actor_owner] = ()
        super().reset(tick=tick, initial_elixir=initial_elixir)

    def update(self, observation):
        if observation.owner != self.actor_owner or observation.tier != ObservationTier.FAIR:
            raise ValueError("public tracker observation belongs to a different actor")
        validate_fair_view(observation, self.actor_owner)
        super().update(observation)

    _event_visible_to_observer = staticmethod(event_visible_to_owner)

    def _record_card_play(self, owner, card_id, *, exact_cost=None):
        if owner != self.actor_owner and card_id not in self._card_states[owner]:
            if card_id not in self.card_costs or len(self._card_states[owner]) >= 8:
                raise ValueError("public opponent reveal violates the card catalog or deck capacity")
            self.decks[owner] = (*self.decks[owner], card_id)
            self._card_states[owner][card_id] = PublicCardState(
                availability=AvailabilityIndex.UNKNOWN_INITIAL, cycle_distance=0, initial_state_known=False)
        super()._record_card_play(owner, card_id, exact_cost=exact_cost)


def build_fair_tensorizer(observation):
    if observation.tier != ObservationTier.FAIR or observation.owner not in (0, 1):
        raise ValueError("tensorizer initialization requires an actor FAIR observation")
    owner = observation.owner
    validate_fair_view(observation, owner)
    own = next(player for player in observation.players if player.owner == owner)
    if any(
            int(runtime.get("form_code", -1)) != 0
            for runtime in own.metadata.get("hand_runtime_by_slot", {}).values()):
        raise ValueError("final v1 tensorizer requires base card forms")
    bundle = production_semantic_bundle()
    tracker = FairPublicTracker(owner=owner, own_deck=own.deck,
        card_costs={card_id: float(spec.elixir_cost) for card_id, spec in bundle.card_specs.items()
            if spec.elixir_cost is not None})
    return UniversalObservationTensorizerV4(actor_owner=owner, card_catalog=bundle.card_catalog,
        ability_catalog=bundle.ability_catalog, entity_archetype_catalog=bundle.entity_archetype_catalog,
        effect_catalog=bundle.effect_catalog, card_specs=bundle.card_specs, deck=own.deck, tracker=tracker,
        deck_roles={card: (False, False) for card in own.deck}, reject_unknown_public_semantics=True)
