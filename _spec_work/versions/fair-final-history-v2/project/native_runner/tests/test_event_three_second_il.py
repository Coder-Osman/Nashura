from dataclasses import replace

import pytest

from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_il_clock import EventILClock
from native_runner.training.event_producer import build_event_label
from native_runner.tests.test_event_producer import _play


def test_new_timing_is_explicit_and_old_timing_is_preserved():
    old, new = EventPolicyConfigV1(), EventPolicyConfigV1.three_second()
    assert (old.delay_bin_count, old.max_wait_ticks) == (61, 300)
    assert (new.delay_bin_count, new.max_wait_ticks) == (31, 60)
    assert new.delay_offset_ms == tuple(range(0, 3001, 100))
    with pytest.raises(ValueError):
        replace(new, delay_offset_ms=(0, 3000))


def test_label_beyond_three_seconds_is_wait_not_clamped_play():
    config = EventPolicyConfigV1.three_second()
    assert not build_event_label([_play(160, 0, 11, 0, (3, 4))],
                                owner=0, event_tick=100, config=config).wait
    assert build_event_label([_play(161, 0, 11, 0, (3, 4))],
                             owner=0, event_tick=100, config=config).wait


def test_wait_repeats_at_three_six_nine_seconds_until_terminal():
    clock = EventILClock(0)
    for tick in (0, 60, 120, 180):
        assert clock.wake(tick) == {0, 1}
        for owner in (0, 1):
            clock.decide(owner, tick)
        assert not clock.wake(tick + 59)
        assert clock.next_tick(tick, [], 1000) == tick + 60
    assert not clock.wake(240, terminal=True)


@pytest.mark.parametrize("public_kind", ["play_card", "activate_ability"])
def test_public_event_resets_timeout_from_new_decision(public_kind):
    clock = EventILClock(0)
    for owner in (0, 1):
        clock.decide(owner, 0)
    assert clock.wake(20, public=public_kind in ("play_card", "activate_ability")) == {0, 1}
    for owner in (0, 1):
        clock.decide(owner, 20)
    assert not clock.wake(60)
    assert clock.next_tick(20, [], 1000) == 80
    assert clock.wake(80) == {0, 1}


def test_players_keep_independent_wait_deadlines_and_terminal_wins():
    clock = EventILClock(0)
    clock.decide(0, 0)
    clock.decide(1, 20)
    assert clock.wake(60) == {0}
    assert clock.wake(60, public=True) == {0, 1}
    assert clock.wake(60, public=True, terminal=True) == set()
    clock.decide(0, 60)
    assert clock.next_tick(60, [70, 71], 1000) == 70
    assert clock.wake(80) == {1}
