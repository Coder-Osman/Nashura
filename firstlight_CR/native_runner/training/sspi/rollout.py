"""SSPI FAIR decision construction over the shared native rollout runtime."""

from .protocol import HandState, HogRefillClock, SSPIInformation, SSPILegality


WAIT_TICKS = 20


def make_sspi_information(environment, observation, frame, owner, *, catalog, refill_clock=None):
    if observation.owner != owner or observation.tick != environment.current_tick:
        raise ValueError("SSPI information must use the owner's settled FAIR observation")
    player = next((item for item in environment.raw_observation["players"]
        if int(item["owner"]) == owner), None)
    if player is None:
        raise ValueError("native owner hand state is missing")
    holes = 4 - len(player["hand"])
    remaining = (refill_clock.remaining(owner, observation.tick, holes)
        if refill_clock is not None else None)
    hand = HandState.from_native(player, refill_remaining_ticks=remaining)
    legality = SSPILegality.from_native(environment, observation, frame, owner,
        catalog=catalog, hand_state=hand)
    return SSPIInformation(owner, observation.tick, frame, hand, legality)


def make_sspi_rollout(environment, *, catalog, execution_seed):
    from ..fair_final.rollout import FinalRollout
    clock = HogRefillClock()

    return FinalRollout(environment, catalog=catalog, execution_seed=execution_seed,
        wait_ticks=WAIT_TICKS, wake_refills=False, refill_observer=clock,
        information_factory=lambda environment, observation, frame, owner, *, catalog:
            make_sspi_information(environment, observation, frame, owner, catalog=catalog,
                refill_clock=clock),
        action_adapter=lambda information, action: information.legality.execution_action(action))
