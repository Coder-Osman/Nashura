from pathlib import Path
import sys
from types import SimpleNamespace

sys.path[:0] = [str(Path(__file__).resolve().parent),
    str(Path(__file__).resolve().parent.parent / "fair-final-history-v2/project")]
import pytest
import torch
from native_runner.training.fair_final.policy import LegalPrefixes
from cases import threshold_actions, trials
from verify import expected_trials


def view():
    return SimpleNamespace(legality=LegalPrefixes([26000021],
        torch.zeros((1, 576, 31), dtype=torch.bool)))


@pytest.mark.parametrize("delay", [2, 4, 15, 18, 25, 30])
def test_threshold_and_next_are_legal_and_no_previous_delay_is_sampled(delay):
    information = view()
    information.legality.plays[0, 12:15, delay:] = True
    actions = threshold_actions(information)
    assert [(action.cell, action.delay_slot) for action in actions] == [
        (cell, current) for cell in (12, 14) for current in (delay, delay + 1) if current <= 30]
    assert all(information.legality.legal(action) for action in actions)


def test_no_threshold_uses_explicit_wait_baseline():
    information = view()
    information.legality.plays[0, 12, 1:] = True
    assert threshold_actions(information) == []
    result = list(trials({0: information, 1: information}))
    assert len(result) == 1 and result[0]["kind"] == "both-WAIT-baseline"
    assert all(action.card_id is None for action in result[0]["actions"].values())


def test_nonmonotone_mask_does_not_create_illegal_next_delay():
    information = view()
    information.legality.plays[0, 12, 4] = True
    assert [action.delay_slot for action in threshold_actions(information)] == [4]


def test_owners_submit_independent_legal_actions_and_wait():
    views = {owner: view() for owner in (0, 1)}
    views[0].legality.plays[0, 12, 4:] = True
    views[1].legality.plays[0, 20, 18:] = True
    result = list(trials(views))
    assert len(result) == 4
    for trial in result:
        assert set(trial["actions"]) == {0, 1}
        assert sum(action.card_id is not None for action in trial["actions"].values()) == 1
        assert all(views[owner].legality.legal(action) for owner, action in trial["actions"].items())


def test_archived_mask_reconstructs_exact_registration():
    masks = {}
    for owner in (0, 1):
        information = view()
        information.legality.plays[0, 12, 4:] = True
        masks[str(owner)] = dict(card_ids=[26000021], shape=[1, 576, 31],
            plays_hex=bytes(information.legality.plays.flatten().tolist()).hex())
    assert len(expected_trials(dict(masks=masks))) == 4
    masks["0"]["plays_hex"] = "00"
    with pytest.raises(ValueError, match="dimensions"):
        expected_trials(dict(masks=masks))
