from native_runner.contracts import ObservationTier


def event_visible_to_owner(event, owner):
    if type(owner) is not int or owner not in (0, 1) or event.data.get("oracle_only") is True:
        return False
    private_to = event.data.get("private_to")
    if private_to is not None and (type(private_to) is not int or private_to != owner):
        return False
    combat = getattr(event, "combat", None)
    if combat is not None and combat.visible_by_owner:
        return combat.visible_by_owner.get(str(owner)) is True
    return True


def validate_fair_view(observation, owner):
    if observation.tier != ObservationTier.FAIR or observation.owner != owner or owner not in (0, 1):
        raise ValueError("FAIR observation belongs to a different actor")
    players = {player.owner: player for player in observation.players}
    if len(observation.players) != 2 or set(players) != {0, 1}:
        raise ValueError("FAIR observation requires both player identities")
    enemy = players[1 - owner]
    if (enemy.private_state_visible or enemy.hand or enemy.cycle or enemy.deck
            or enemy.next_card is not None or enemy.elixir_exact is not None
            or enemy.elixir_visible is not None or enemy.evolution_state or enemy.ability_state
            or enemy.ability_runtime_states or enemy.evolution_runtime_states or enemy.metadata):
        raise ValueError("opponent private state reached the FAIR tensorizer boundary")
    if observation.native_digest is not None:
        raise ValueError("native audit digest reached the FAIR tensorizer boundary")
