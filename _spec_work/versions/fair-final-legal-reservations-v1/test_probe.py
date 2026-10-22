import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import torch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.with_name("fair-final-history-v2") / "project"))
spec = importlib.util.spec_from_file_location("legal_reservation_probe", ROOT / "probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
from native_runner.training.fair_final.policy import LegalPrefixes


def information(owner):
    mask = torch.zeros((2, 576, 31), dtype=torch.bool)
    mask[:, 100, 1] = True
    mask[:, 200, 1] = True
    mask[:, 250, 30] = True
    mask[:, 300, 30] = True
    mask[0, 100 if owner == 0 else 475, 1] = True
    return SimpleNamespace(owner=owner, legality=LegalPrefixes([27000000, 26000000], mask))


def test_choices_cover_both_delays_for_every_legal_card():
    info = information(0)
    actions = probe.choices(info)
    assert [(action.card_id, action.delay_slot) for action in actions] == [
        (27000000, 1), (27000000, 30), (26000000, 1), (26000000, 30)]
    assert all(info.legality.legal(action) for action in actions)
    assert [action.cell for action in actions] == [100, 300, 100, 300]


def test_no_illegal_fallback_when_no_act_is_available():
    info = information(0)
    info.legality = LegalPrefixes([27000000], torch.zeros((1, 576, 31), dtype=torch.bool))
    assert probe.choices(info) == []
    assert list(probe.trials({0: info, 1: info})) == []


def test_trial_submits_complete_simultaneous_legal_views():
    views = {owner: information(owner) for owner in (0, 1)}
    trials = list(probe.trials(views))
    assert len(trials) == 11
    assert {trial["kind"] for trial in trials} == {"single-act", "simultaneous-act", "mirrored-cell-cannon"}
    for trial in trials:
        assert set(trial["actions"]) == {0, 1}
        assert all(views[owner].legality.legal(action) for owner, action in trial["actions"].items())
    cannon = next(trial for trial in trials if trial["kind"] == "mirrored-cell-cannon")
    assert cannon["actions"][0].cell == 575 - cannon["actions"][1].cell
