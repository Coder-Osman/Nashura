"""SSPI FAIR decision construction over the shared native rollout runtime."""

from .protocol import HandState, SSPIInformation, SSPILegality


WAIT_TICKS = 20


def make_sspi_information(environment, observation, frame, owner, *, catalog):
    if observation.owner != owner or observation.tick != environment.current_tick:
        raise ValueError("SSPI information must use the owner's settled FAIR observation")
    player = next((item for item in environment.raw_observation["players"]
        if int(item["owner"]) == owner), None)
    if player is None:
        raise ValueError("native owner hand state is missing")
    hand = HandState.from_native(player)
    legality = SSPILegality.from_native(environment, observation, frame, owner,
        catalog=catalog, hand_state=hand)
    return SSPIInformation(owner, observation.tick, frame, hand, legality)


def make_sspi_rollout(environment, *, catalog, execution_seed):
    from ..fair_final.rollout import FinalRollout

    return FinalRollout(environment, catalog=catalog, execution_seed=execution_seed,
        wait_ticks=WAIT_TICKS, wake_refills=True, information_factory=make_sspi_information)
