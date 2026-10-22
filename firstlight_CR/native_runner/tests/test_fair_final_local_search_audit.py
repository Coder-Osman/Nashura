from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_final_prior_search_audit import NativeAuditWorld
from native_runner.tests.test_fair_final_search import search_fixture
from tools import probe_fair_final_local_search as probe


def instrument(search, monkeypatch):
    original = search.belief
    worlds = []
    session = SimpleNamespace(root=SimpleNamespace(native=SimpleNamespace(handle=23)), profile=dict(ticks=0))
    provider = SimpleNamespace(sampler_sha=original.sampler_sha, cursor=SimpleNamespace(session=session), active=None)

    def build(info, *, seed):
        world = NativeAuditWorld(original.build(info, seed=seed))
        worlds.append(world)
        world.local_profile = dict(seconds=.1, cycle_delta=dict(added_plays=0), anchor_event=dict(mode="cold"))
        world.rollout.freeze_due_information = lambda: {info.owner: info}
        world.rollout.phase_error = None
        world.rollout.phase_corrupt = False
        close = world.close

        def release():
            try:
                close()
            finally:
                provider.active = None

        world.close = release
        provider.active = world
        return world

    def phases(rollout):
        if rollout.phase_error is not None:
            raise rollout.phase_error
        return {"17": dict(deploy_previous_ms=99 if rollout.phase_corrupt else 0)}

    provider.build = build
    monkeypatch.setattr(probe, "phases", phases)
    search.belief = probe.LocalAuditProvider(provider)
    return search.belief, worlds


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("options", [{}, {"fail_first": 3}, {"incomplete_after": 1}, {"terminal": -1.}])
def test_local_phase_audit_preserves_full128_and_retains_anchor(monkeypatch, owner, options):
    baseline, info, arguments = search_fixture(owner=owner, **options)
    search, _, _ = search_fixture(owner=owner, **options)
    audited, worlds = instrument(search, monkeypatch)
    expected = baseline.run(info, **arguments)
    actual = search.run(info, **arguments)
    assert actual.result == expected.result and actual.selected == expected.selected
    assert actual.policy_target == expected.policy_target
    assert audited.lifecycle["builds"] == len(worlds) == 1
    assert audited.lifecycle["restores"] == actual.audit["attempts"]+3
    assert audited.lifecycle["phase_restores"] == actual.audit["attempts"]+2
    assert audited.lifecycle["phase_object_count"] == 1
    assert audited.lifecycle["closed"] and audited.lifecycle["snapshot_released"]
    assert audited.anchor_retained and audited.anchor_restored_sha == info.sha256
    assert audited.provider.active is None and worlds[0].close_calls == 1
    assert sum(attempt["decisions"] > 0 for attempt in audited.attempts) == 128


@pytest.mark.parametrize("failure", ["phase-read", "phase-mismatch", "anchor-lost", "anchor-changed"])
def test_local_audit_failure_releases_search_lease(monkeypatch, failure):
    search, info, _ = search_fixture()
    audited, worlds = instrument(search, monkeypatch)
    world = audited.build(info, seed=17)
    if failure == "phase-read":
        world.rollout.phase_error = RuntimeError("phase read failed")
        error = RuntimeError
    elif failure == "phase-mismatch":
        world.rollout.phase_corrupt = True
        error = ValueError
    elif failure == "anchor-lost":
        audited.provider.cursor.session.root.native.handle = None
        error = ValueError
    else:
        world.rollout.freeze_due_information = lambda: {info.owner:SimpleNamespace(sha256="0"*64)}
        error = ValueError
    with pytest.raises(error):
        world.close()
    assert world.closed and world.root.native.handle is None
    assert audited.provider.active is None and worlds[0].close_calls == 1
