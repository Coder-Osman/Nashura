import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import torch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.with_name("fair-final-history-v2") / "project"))
spec = importlib.util.spec_from_file_location("legal_probe", ROOT.with_name("fair-final-legal-reservations-v1") / "probe.py")
legal_probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = legal_probe
spec.loader.exec_module(legal_probe)
spec = importlib.util.spec_from_file_location("pocket_probe", ROOT / "probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

from native_runner.arena import card_placement_mask
from native_runner.card_specs import build_card_catalog
from native_runner.paths import WORKSPACE_ROOT
from native_runner.training.fair_final.policy import FullAction


def information(owner, cells, cards=(27000000,)):
    plays = torch.zeros((len(cards), 576, 31), dtype=torch.bool)
    for card, cell, delay in cells:
        plays[cards.index(card), cell, delay] = True
    return SimpleNamespace(owner=owner, legality=SimpleNamespace(card_ids=cards, plays=plays))


def test_collision_rotates_player_one_and_requires_legal_both():
    cell = 18 * 18 + 2
    views = {0: information(0, [(27000000, cell, 1)]),
        1: information(1, [(27000000, 575 - cell, 1)])}
    assert probe.collision_actions(views, 1) == {
        0: FullAction(27000000, cell, 1), 1: FullAction(27000000, 575 - cell, 1)}
    assert probe.collision_actions(views, 30) is None
    assert probe.collision_actions({0: views[0]}, 1) is None


def test_edge_contact_is_not_collision():
    cell = 18 * 18 + 2
    views = {0: information(0, [(27000000, cell, 1)]),
        1: information(1, [(27000000, 575 - cell - 3, 1)])}
    assert probe.collision_actions(views, 1) is None


def test_trajectory_preserves_cannon_and_stops_attack_after_pocket():
    cards = (probe.CANNON, probe.HOG)
    info = information(1, [(probe.CANNON, 1, 30), (probe.HOG, 1, 1)], cards)
    assert probe.trajectory_action(info, pocket_open=False) == FullAction()
    info.owner = 0
    assert probe.trajectory_action(info, pocket_open=True) == FullAction()
    assert probe.trajectory_action(info, pocket_open=False).card_id == probe.HOG


def test_frozen_cannon_geometry_overlaps_only_after_tower_loss():
    cannon = next(card for card in build_card_catalog(WORKSPACE_ROOT).specs if card.card_id == probe.CANNON)
    for lanes, expected in (((), False), (("left",), True)):
        views = {}
        for owner in (0, 1):
            mask = card_placement_mask(cannon, owner, ruleset_id="a" * 64,
                destroyed_enemy_princess_lanes=lanes if owner == 0 else ())
            cells = [((x + y * 18) if owner == 0 else (575 - x - y * 18))
                for y, row in enumerate(mask.rows) for x, legal in enumerate(row) if legal]
            views[owner] = information(owner, [(probe.CANNON, cell, 1) for cell in cells])
        assert (probe.collision_actions(views, 1) is not None) == expected
