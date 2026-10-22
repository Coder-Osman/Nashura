from types import SimpleNamespace

import pytest

from native_runner.tests.test_fair_final_search import search_fixture
from native_runner.training.fair_search.contracts import semantic_digest
from tools.probe_fair_final_prior_search import PriorAuditProvider


class NativeAuditWorld:
    def __init__(self, world):
        self.world = world
        self.root = SimpleNamespace(native=SimpleNamespace(handle=17))
        self.fail_digest = False
        self.close_calls = 0
        self.rollout = SimpleNamespace(runtime=SimpleNamespace(environment=SimpleNamespace(
            native=SimpleNamespace(_request=self.request))))

    def __getattr__(self, name):
        return getattr(self.world, name)

    def request(self, command):
        assert command == "state-digest"
        if self.fail_digest:
            raise RuntimeError("digest read failed")
        return dict(sha256=semantic_digest([self.world.current.sha256, self.world.depth]))

    def close(self):
        self.close_calls += 1
        self.world.close()
        self.root.native.handle = None


def instrument(search):
    original = search.belief
    build = original.build
    worlds = []

    def wrapped(information, *, seed):
        world = NativeAuditWorld(build(information, seed=seed))
        worlds.append(world)
        return world

    search.belief = PriorAuditProvider(SimpleNamespace(sampler_sha=original.sampler_sha, build=wrapped))
    return search.belief, worlds


@pytest.mark.parametrize("owner", [0, 1])
@pytest.mark.parametrize("options", [{}, {"fail_first": 3}, {"incomplete_after": 1}, {"terminal": -1.}])
def test_native_digest_audit_preserves_full128_search(owner, options):
    baseline, info, arguments = search_fixture(owner=owner, **options)
    search, _, _ = search_fixture(owner=owner, **options)
    audited, worlds = instrument(search)
    expected = baseline.run(info, **arguments)
    actual = search.run(info, **arguments)
    assert actual.result == expected.result
    assert actual.selected == expected.selected and actual.policy_target == expected.policy_target
    assert audited.lifecycle["builds"] == 1 and len(worlds) == 1
    assert audited.lifecycle["restores"] == actual.audit["attempts"]+2
    assert audited.lifecycle["closed"] and audited.lifecycle["snapshot_released"]
    assert worlds[0].close_calls == 1
    assert sum(attempt["decisions"] > 0 for attempt in audited.attempts) == 128


@pytest.mark.parametrize("failure", ["initial-digest", "close-digest", "restore-mismatch"])
def test_prior_audit_failure_still_releases_world(failure):
    search, info, _ = search_fixture()
    world = NativeAuditWorld(search.belief.build(info, seed=17))
    audited = PriorAuditProvider(SimpleNamespace(sampler_sha=search.belief.sampler_sha,
        build=lambda information, seed: world))
    if failure == "initial-digest":
        world.fail_digest = True
        with pytest.raises(RuntimeError, match="digest read"):
            audited.build(info, seed=17)
    else:
        audited.build(info, seed=17)
        if failure == "close-digest":
            world.fail_digest = True
            error = RuntimeError
        else:
            audited.lifecycle["root_digest"] = "0"*64
            error = ValueError
        with pytest.raises(error):
            world.close()
    assert world.closed and world.root.native.handle is None and world.close_calls == 1
