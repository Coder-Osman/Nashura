from dataclasses import replace
from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_search import frame, information
from native_runner.training.fair_search.native import NativeReplayFactory


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_conditional_update_retries_from_snapshot_and_never_rebuilds(information, monkeypatch, repair_succeeds):
    from native_runner.training.v4 import factory as semantic_factory

    monkeypatch.setattr(semantic_factory, "production_semantic_bundle", lambda: SimpleNamespace(card_catalog=None))
    previous = replace(information, tick=information.tick - 1)
    proposed = SimpleNamespace(selected_initial={0: "own", 1: "sampled"})
    pool = SimpleNamespace(propose=lambda *args, **kwargs: (proposed, "queues"))
    events = []
    world = SimpleNamespace(
        root_information=lambda: previous, draw_pool=pool, draw_seed=10, draw_initial="previous",
        runtime=object(), trace=[], restore_root=lambda: events.append("restore"),
        condition_draws=lambda *args: events.append("condition"), replace_root=lambda: events.append("replace"))
    factory = NativeReplayFactory([1], {}, {}, None, conditional_draws=True)

    def replay(*args, **kwargs):
        events.append("replay")
        if events.count("replay") == 1 or not repair_succeeds:
            raise ValueError("native FAIR mismatch")

    factory._replay = replay
    if repair_succeeds:
        factory.update(world, information)
        assert events == ["restore", "replay", "restore", "condition", "replay", "replace"]
        assert world.draw_pool is proposed
        assert world.draw_initial == proposed.selected_initial
        assert factory.conditioned_roots == 1
    else:
        with pytest.raises(ValueError, match="native FAIR mismatch"):
            factory.update(world, information)
        assert events == ["restore", "replay", "restore", "condition", "replay", "restore"]
        assert world.draw_pool is pool
        assert world.draw_initial == "previous"
        assert factory.conditioned_roots == 0


def test_changed_information_prefix_fails_without_reconstruction(information):
    previous = replace(information, own_history=("previous",))
    world = SimpleNamespace(root_information=lambda: previous)
    factory = NativeReplayFactory([1], {}, {}, None, conditional_draws=True)
    with pytest.raises(ValueError, match="does not extend"):
        factory.update(world, information)
