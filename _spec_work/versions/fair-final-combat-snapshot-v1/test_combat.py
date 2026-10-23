from copy import deepcopy
from dataclasses import asdict
import hashlib
from types import SimpleNamespace
import zlib

import pytest
import torch

from combat_protocol import choose, mask_record, validate_selection
from verify_combat import validate_steps
from native_runner.training.fair_final.policy import FullAction, LegalPrefixes
from native_runner.training.fair_final.timing import DecisionClock
from native_runner.training.fair_search.contracts import semantic_digest


def information(tick=90, *, playable=True):
    plays = torch.zeros((2, 576, 31), dtype=torch.bool)
    if playable:
        plays[0, 14 * 18 + 3, 1] = True
        plays[0, 14 * 18 + 4, 30] = True
        plays[1, 10 * 18 + 3, 1:31] = True
    return SimpleNamespace(tick=tick,
        legality=LegalPrefixes((26000021, 26000014), plays))


def selection(action=FullAction(26000021, 14 * 18 + 3, 1)):
    return dict(action=asdict(action), mask=mask_record(information()), fair_sha="1" * 64)


@pytest.mark.parametrize("tick,expected", [(90, FullAction(26000021, 255, 1)),
    (91, FullAction(26000021, 256, 30))])
def test_choice_obeys_priority_and_delay(tick, expected):
    info = information(tick)
    assert choose(info) == expected
    validate_selection(dict(action=asdict(expected), mask=mask_record(info)))


def test_wait_when_no_legal_play():
    info = information(playable=False)
    assert choose(info) == FullAction()
    validate_selection(dict(action=asdict(choose(info)), mask=mask_record(info)))


@pytest.mark.parametrize("change", [
    lambda row: row["action"].update(cell=254),
    lambda row: row["action"].update(delay_slot=0),
    lambda row: row["action"].update(card_id=True),
    lambda row: row["action"].update(cell=True),
    lambda row: row["action"].update(delay_slot=True),
    lambda row: row["action"].update(extra=1),
    lambda row: row["mask"].update(shape=[2, 576, 31.0]),
    lambda row: row["mask"].update(card_ids=[26000021, 26000021]),
    lambda row: row["mask"].update(modes=[True, False]),
    lambda row: row["mask"].update(modes=[1, True]),
    lambda row: row["mask"].update(plays_sha256="0" * 64),
    lambda row: row["mask"].update(plays_zlib_hex=row["mask"]["plays_zlib_hex"] + "00"),
    lambda row: row["mask"].update(plays_zlib_hex=zlib.compress(bytes(2 * 576 * 31 + 1)).hex()),
])
def test_illegal_or_corrupt_selection_rejected(change):
    row = selection()
    change(row)
    with pytest.raises(ValueError):
        validate_selection(row)


def test_disallowed_wait_rejected():
    row = selection(FullAction())
    row["mask"]["modes"][0] = False
    with pytest.raises(ValueError):
        validate_selection(row)


def test_boolean_mask_bytes_rejected_even_with_rehashed_content():
    row = selection()
    content = bytearray(zlib.decompress(bytes.fromhex(row["mask"]["plays_zlib_hex"])))
    content[10] = 2
    row["mask"].update(plays_zlib_hex=zlib.compress(content).hex(), plays_sha256=hashlib.sha256(content).hexdigest())
    with pytest.raises(ValueError):
        validate_selection(row)


def cancellation_trace():
    clock = DecisionClock()
    clock.begin_tick(90, wake_owners=(0, 1), adjudicate_due=lambda *_: {}, freeze=lambda *_: None)

    def state():
        return dict(tick=clock.tick, digest=dict(tick=clock.tick, scope="native-payload+global-mt2504.v1",
            sha256="2" * 64, nativeSha256="3" * 64), clock_sha=semantic_digest(clock.capture()),
            global_sha="4" * 64, ready=sorted(clock.ready), next_identity=clock.next_identity,
            pending=[asdict(plan) for plan in clock.pending.values()],
            fair_sha={str(owner): "1" * 64 for owner in clock.ready},
            rng_sha={"0": "5" * 64, "1": "6" * 64}, public=dict(towers=[], entities=[]))

    opening = state()
    actions = {0: FullAction(26000021, 255, 1), 1: FullAction(26000021, 256, 30)}
    clock.commit(actions, legal=lambda *_: True)
    committed = [asdict(plan) for plan in clock.pending.values()]
    first = dict(decision={str(owner): selection(action) for owner, action in actions.items()},
        committed=committed, outcomes={}, evidence=[])
    clock.begin_tick(91, wake_owners=(), adjudicate_due=lambda *_: {}, freeze=lambda *_: None)
    first["after"] = state()
    clock.begin_tick(92, wake_owners=(0, 1), adjudicate_due=lambda *_: {0: "executed"}, freeze=lambda *_: None)
    second = dict(decision={}, committed=[], outcomes={"0": "executed"},
        evidence=deepcopy(clock.evidence), after=state())
    return opening, [first, second]


def test_normal_preemption_retained():
    opening, steps = cancellation_trace()
    final, history = validate_steps(opening, steps, [])
    assert final["ready"] == [0, 1]
    assert [event["reason"] for event in history] == ["executed", "preempted"]


@pytest.mark.parametrize("change", [
    lambda steps: steps[1]["evidence"].pop(),
    lambda steps: steps[1]["after"].update(tick=93),
    lambda steps: steps[0]["decision"].pop("1"),
    lambda steps: steps[0]["committed"][1].update(due_tick=91),
    lambda steps: steps[1].update(outcomes={}),
    lambda steps: steps[1]["after"].update(ready=[0]),
    lambda steps: steps[1]["after"]["rng_sha"].update({"0": "7" * 64}),
    lambda steps: steps[0]["after"].update(clock_sha="8" * 64),
])
def test_semantic_trajectory_corruption_rejected(change):
    opening, steps = cancellation_trace()
    change(steps)
    with pytest.raises(ValueError):
        validate_steps(opening, steps, [])


def test_replayed_branch_from_pending_root():
    opening, steps = cancellation_trace()
    root, history = validate_steps(opening, steps[:1], [])
    assert len(root["pending"]) == 2
    assert validate_steps(root, steps[1:], history) == validate_steps(opening, steps, [])
