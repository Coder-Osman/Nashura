import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import general_provider
from probe_native_general import NativeGeneralProvider
import run_native_general
import verify_native_general
from native_runner.tests.test_fair_final_history_provider import roots


def test_native_adapter_only_exposes_local_handles(monkeypatch):
    inner = SimpleNamespace(root=object(), rollout=object(), local_profile={"seconds": 1.})
    world = SimpleNamespace(world=inner, manifest={"mode": "local"})
    monkeypatch.setattr(general_provider.GeneralHistorySearchProvider, "build",
        lambda *args, **kwargs: world)
    provider = object.__new__(NativeGeneralProvider)
    provider.local = SimpleNamespace(cursor=object())
    assert provider.build(None, seed=17) is world
    assert world.root is inner.root and world.rollout is inner.rollout
    assert world.local_profile is inner.local_profile
    assert provider.cursor is provider.local.cursor


def test_native_adapter_never_claims_fallback_coverage(monkeypatch):
    calls = []
    world = SimpleNamespace(manifest={"mode": "independent-history-rebuild"},
        close=lambda: calls.append("closed"))
    monkeypatch.setattr(general_provider.GeneralHistorySearchProvider, "build",
        lambda *args, **kwargs: world)
    with pytest.raises(ValueError, match="local general-provider roots only"):
        object.__new__(NativeGeneralProvider).build(None, seed=17)
    assert calls == ["closed"]


def test_native_payload_keeps_frozen_worker_and_independent_ports(monkeypatch):
    original = (run_native_general.EVIDENCE / "refill_provider_worker.py").read_bytes()
    monkeypatch.setattr(run_native_general.refill_runner, "payloads", lambda *args: {
        "project/run.py": original, "run-manifest.json": json.dumps(dict(jobs=[])).encode()})
    files = run_native_general.payloads("/diagnostic", "a" * 64, {})
    worker = files["project/run.py"]
    assert worker == original.replace(b"project/tools/probe_fair_final_refill_provider.py",
        b"project/probe_native_general.py")
    assert json.loads(files["run-manifest.json"])["jobs"] == run_native_general.JOBS
    assert set(run_native_general.JOBS[0]["ports"]).isdisjoint({29761, 29762})
    for name in ("general_provider.py", "probe_native_general.py"):
        assert files["project/" + name] == (Path(__file__).parent / name).read_bytes()


@pytest.fixture
def wrapped_root():
    from native_runner.training.fair_final.deck_belief import DeckFrequency
    from native_runner.training.fair_search.config import HOG_DECK
    history, _, _, _ = roots()
    prior = verify_native_general.FrozenDeckPrior("fixed-experiment", "e" * 64,
        (DeckFrequency(tuple(sorted(HOG_DECK)), 1),))
    opponent = SimpleNamespace(checkpoint_sha=verify_native_general.SOURCE_SHA,
        controller_sha=verify_native_general.semantic_digest(dict(source=verify_native_general.SOURCE_SHA,
            adapter="fair-final-source-a", temperature=1.)))
    factory = verify_native_general.IncrementalRefillBeliefFactory(None, prior=prior,
        catalog=None, opponent=opponent, sidecar_factory=None)
    provider = general_provider.GeneralHistorySearchProvider(factory, history=history,
        anchor_seed=verify_native_general.ANCHOR_SEED)
    local = provider._local_provider()
    construction = dict(sampler_sha=local.sampler_sha, source="independent-belief",
        actual_state_copied=False, root_fair_sha=history.root_fair_sha, history_sha=history.sha256,
        anchor_history_sha=history.sha256, root_sampling="fresh-draw-on-independent-incremental-anchor",
        training_admissible=False, conditioning=dict(schema=local.conditioner.schema,
            sampler_sha=local.conditioner.sampler_sha, root_fair_sha=history.root_fair_sha,
            history_sha=history.sha256, actual_state_copied=False, history_ticks_replayed=0,
            native_draw_verified=True, public_and_own_fair_unchanged=True, controller_unchanged=True,
            training_admissible=False), anchor=dict(sampler_sha=factory.sampler_sha,
            source="independent-belief", actual_state_copied=False, root_fair_sha=history.root_fair_sha,
            history_sha=history.sha256, native_draw_verified=True, training_admissible=False,
            opponent_checkpoint_sha=opponent.checkpoint_sha, opponent_controller_sha=opponent.controller_sha))
    limits = verify_native_general.SearchLimits()
    result = dict(schema="SearchResult/v1", valid_simulations=128, unfinished_edge_backups=0,
        root_player=history.opening.owner, value_perspective=history.opening.owner,
        root_fair_state_sha=history.root_fair_sha, search_protocol_sha=limits.sha256,
        world_sampler_sha=provider.sampler_sha, opponent_controller_sha=opponent.controller_sha,
        noise_fraction=0., invalid_attempts=0, mode_mass=[1., 0.], candidates=[dict(
            action_id=0, visits=128, q=0., original_joint_logp=0., prior_card=1.,
            prior_projected=1., prior_search=1., proposal="WAIT")])
    row = dict(owner=history.opening.owner, tick=history.root_tick, fair_sha=history.root_fair_sha,
        history_sha=history.sha256, lease_released=True, anchor_retained=True,
        anchor_restored_sha=history.root_fair_sha, actual_digest_before="a" * 64,
        actual_digest_after="a" * 64, altered=False, mutation=None,
        outcome=dict(result=result, selected="wait", target=[[0, 128]]),
        local_profile=dict(seconds=.1, cycle_delta=dict(added_plays=0, order_checks=0)),
        world_lifecycle=dict(builds=1, restores=131, closed=True, snapshot_released=True,
            root_digest="b" * 64, phase_object_count=1, phase_restores=130),
        audit=dict(seconds=5., timings=dict(belief=1., restore=1., inference=1., transition=1.),
            attempts=128, discarded_edges=0, completed_lifecycle=dict(wait_expired=128)),
        attempts=[dict(decisions=1, ticks=60, transitions=[dict(completed=True, decisions=1, ticks=60,
            before_tick=history.root_tick, before_sha=history.root_fair_sha,
            after_tick=history.root_tick + 60, after_sha="c" * 64, reason="wait_expired", terminal=None,
            hard_tick=history.root_tick + limits.soft_ticks + limits.extension_ticks,
            maximum_decisions=limits.soft_decisions + limits.extension_decisions)]) for _ in range(128)])
    row["audit"]["world_manifest"] = dict(schema="fair-final-general-history-world.v1",
        source="independent-belief", actual_state_copied=False, root_fair_sha=history.root_fair_sha,
        history_sha=history.sha256, sampler_sha=provider.sampler_sha,
        seed_commitment=verify_native_general.semantic_digest(
            [verify_native_general.split_seed(verify_native_general.SEARCH_SEED, "world")]),
        mode="local", fallback_reason=None, construction=construction,
        scope="bounded-general-history-engineering", training_admissible=False)
    row["outcome"]["result"]["world_sampler_sha"] = provider.sampler_sha
    row["outcome"]["result"]["world_commitment"] = verify_native_general.semantic_digest(
        row["audit"]["world_manifest"])
    yield row, history, provider, opponent
    provider.close()
    factory.close()


def test_world_verifier_accepts_synthetic_fixture(wrapped_root):
    verify_native_general.validate_world(*wrapped_root)


@pytest.mark.parametrize("field,value", [("mode", "independent-history-rebuild"),
    ("actual_state_copied", True), ("actual_state_copied", 0),
    ("training_admissible", True), ("sampler_sha", "0" * 64),
    ("history_sha", "0" * 64), ("fallback_reason", "fabricated")])
def test_world_verifier_rejects_scope_and_identity_tampering(wrapped_root, field, value):
    row, history, provider, opponent = wrapped_root
    row["audit"]["world_manifest"][field] = value
    with pytest.raises(ValueError):
        verify_native_general.validate_world(row, history, provider, opponent)


@pytest.mark.parametrize("kind", ["visits", "unfinished", "digest", "sampler", "conditioning"])
def test_world_verifier_rejects_search_and_restore_tampering(wrapped_root, kind):
    row, history, provider, opponent = wrapped_root
    if kind == "visits":
        row["outcome"]["result"]["candidates"][0]["visits"] += 1
    elif kind == "unfinished":
        row["outcome"]["result"]["unfinished_edge_backups"] = 1
    elif kind == "digest":
        row["actual_digest_after"] = "0" * 64
    elif kind == "sampler":
        row["outcome"]["result"]["world_sampler_sha"] = "0" * 64
    else:
        row["audit"]["world_manifest"]["construction"]["conditioning"]["history_ticks_replayed"] = 1
    with pytest.raises(ValueError):
        verify_native_general.validate_world(row, history, provider, opponent)
