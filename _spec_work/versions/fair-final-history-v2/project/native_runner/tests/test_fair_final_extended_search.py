from types import SimpleNamespace

import pytest

from native_runner.training.fair_final.search import Transition
from native_runner.tests.test_fair_final_search import search_fixture
from tools.probe_fair_final_extended_search import BoundaryAuditProvider


class AuditWorld:
    def __init__(self, result):
        self.result = result
        self.tick = 100
        self.restore_calls = 0
        self.transition_calls = []

    def root_owner_information(self):
        if self.result.terminal_value is not None and self.transition_calls:
            raise ValueError("terminal has no decision boundary")
        return SimpleNamespace(tick=self.tick, sha256=str(self.tick))

    def restore_root(self):
        self.tick = 100
        self.restore_calls += 1

    def transition(self, action, **kwargs):
        self.transition_calls.append((action, kwargs))
        if self.result.completed:
            self.tick += self.result.ticks
        return self.result


@pytest.mark.parametrize("result", [Transition(True, 1, 12, reason="executed"),
    Transition(False, 3, 60), Transition(True, 1, 6, terminal_value=-1., reason="terminal_expired")])
def test_boundary_audit_does_not_change_transition_or_restore_semantics(result):
    world = AuditWorld(result)
    provider = SimpleNamespace(sampler_sha="a" * 64, build=lambda information, seed: world)
    audited = BoundaryAuditProvider(provider)
    assert audited.sampler_sha == provider.sampler_sha
    assert audited.build(object(), seed=7) is world
    world.restore_root()
    arguments = dict(hard_tick=360, maximum_decisions=12, generator=object())
    assert world.transition("act", **arguments) is result
    assert world.transition_calls == [("act", arguments)]
    assert len(audited.attempts) == 1
    attempt = audited.attempts[0]
    assert attempt["decisions"] == (result.decisions if result.completed else 0)
    assert attempt["ticks"] == (result.ticks if result.completed else 0)
    event = attempt["transitions"][0]
    assert event["before_tick"] == 100
    assert event["after_tick"] == (None if result.terminal_value is not None else world.tick)
    assert event["hard_tick"] == 360 and event["maximum_decisions"] == 12
    world.restore_root()
    assert world.restore_calls == 2 and world.tick == 100


def test_boundary_audit_groups_edges_by_actual_simulation_restore():
    world = AuditWorld(Transition(True, 1, 8, reason="preempted"))
    audited = BoundaryAuditProvider(SimpleNamespace(sampler_sha="a" * 64, build=lambda info, seed: world))
    audited.build(object(), seed=7)
    world.restore_root()
    world.restore_root()
    assert not audited.attempts
    for count in (3, 2):
        for index in range(count):
            world.transition("act", hard_tick=360, maximum_decisions=12-index)
        world.restore_root()
    assert [attempt["decisions"] for attempt in audited.attempts] == [3, 2]
    assert [attempt["ticks"] for attempt in audited.attempts] == [24, 16]
    assert [len(attempt["transitions"]) for attempt in audited.attempts] == [3, 2]


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("options", [{}, {"fail_first": 3}, {"incomplete_after": 1}, {"terminal": -1.}])
def test_audited_full128_matches_uninstrumented_search(owner, options):
    baseline, information, arguments = search_fixture(owner=owner, **options)
    instrumented, _, _ = search_fixture(owner=owner, **options)
    provider = instrumented.belief
    instrumented.belief = BoundaryAuditProvider(provider)
    expected = baseline.run(information, **arguments)
    actual = instrumented.run(information, **arguments)
    assert actual.result == expected.result
    assert actual.selected == expected.selected
    assert actual.policy_target == expected.policy_target
    assert provider.world.closed and baseline.belief.world.closed
    attempts = instrumented.belief.attempts
    assert len(attempts) == actual.audit["attempts"]
    assert sum(attempt["decisions"] > 0 for attempt in attempts) == 128
    assert sum(not edge["completed"] for attempt in attempts for edge in attempt["transitions"]) == actual.audit["discarded_edges"]
