from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
import torch

from native_runner.training.event_opponents import QUOTAS, freeze_plan, match_metadata, validate_shard_ownership
from native_runner.training.mixed_decks import DECKS, DECK_IDS, stage, select_pool, build_plan, validate_plan
from native_runner.training.event_ppo import (
    EventPpoConfigV1, event_ppo_update, objective_row_weights, normalize_deck_advantages,
)
from native_runner.training.event_batches import PreparedEventRollout
from native_runner.tests.test_event_model import _event_model
from native_runner.tests.test_event_ppo import _play_lane
from native_runner.tests.test_event_ppo_weighting import RecordingSGD
from native_runner.tests.test_event_batches import clone_model

FLEET = {h: dict(ports=list(range(100, 100 + a + b))) for h, (a, b) in QUOTAS.items()}
BASELINE = dict(iteration=1250, sha256="a" * 64)
FIXED = [dict(iteration=n, sha256=f"{n:064x}") for n in (600, 645)]
CONSTRAINTS = {"delay": {"minimum_ms": 100}}


def plan(round_number=1):
    records = [dict(iteration=1250 + n, sha256=f"{1250+n:064x}") for n in range(1, round_number)]
    pool = select_pool(records, round_number=round_number, start_iteration=1250, baseline=BASELINE, fixed=FIXED)
    return build_plan(iteration=1250 + round_number, current_sha=BASELINE["sha256"] if round_number == 1 else "b" * 64,
                      seed_base=871231, pool=pool, fleet=FLEET, start_iteration=1250, action_constraints=CONSTRAINTS)


def test_every_curriculum_round_and_swapped_history_identity():
    totals = Counter()
    history_decks = Counter()
    for n in range(1, 101):
        p = validate_plan(plan(n))
        assert p["learning_weights"] == stage(n)[1]
        assert len(p["matches"]) == 108
        self_games = [m for m in p["matches"] if m["kind"] == "self"]
        assert len(self_games) == 72
        cells = Counter(tuple(m["owner_deck_ids"]) for m in self_games)
        assert len(cells) == 16 and set(cells.values()) == {4, 5}
        totals.update(cells)
        pairs = defaultdict(list)
        for m in p["matches"]:
            assert m["owner_decks"] == [list(DECKS[d]) for d in m["owner_deck_ids"]]
            if m["kind"] != "self":
                pairs[m["pair_id"]].append(m)
                history_decks[m["owner_deck_ids"][m["learner_owner"]]] += 1
        assert len(pairs) == 18
        for a, b in pairs.values():
            assert a["seed"] == b["seed"]
            assert a["owner_sha256"] == b["owner_sha256"][::-1]
            assert a["owner_decks"] == b["owner_decks"][::-1]
            assert {a["learner_owner"], b["learner_owner"]} == {0, 1}
        assert p["recent_fallback"] == (n <= 11)
        if n == 12:
            assert [r["mixed_round"] for r in p["pool"]["recent"]] == [1]
        if n >= 13:
            assert [r["mixed_round"] for r in p["pool"]["recent"]] == [n-11, n-12]
            recent = [m for m in p["matches"] if m["kind"] == "recent"]
            assert set(Counter(m["opponent"]["sha256"] for m in recent).values()) == {12}
            assert set(Counter(m["owner_deck_ids"][1-m["learner_owner"]] for m in recent).values()) == {6}
    assert set(totals.values()) == {450}
    assert set(history_decks.values()) == {900}


def test_freeze_resume_phase_and_deck_tampering(tmp_path):
    p = plan(21)
    args = dict(builder=build_plan, iteration=p["iteration"], seed_base=p["seed_base"],
                current_sha=p["current_sha256"], pool=p["pool"], fleet=FLEET,
                start_iteration=1250, action_constraints=CONSTRAINTS)
    assert freeze_plan(tmp_path / "plan.json", **args) == p
    assert freeze_plan(tmp_path / "plan.json", **args) == p
    from native_runner.training.event_opponents import digest
    for field, value in (("phase", 1), ("learning_weights", stage(1)[1]), ("action_constraints", {})):
        changed = deepcopy(p)
        changed[field] = value
        changed["sha256"] = digest({k: v for k, v in changed.items() if k != "sha256"})
        with pytest.raises(ValueError):
            validate_plan(changed)


def test_baseline_alias_does_not_admit_frozen_opponent_rows():
    p = plan()
    m = next(m for m in p["matches"] if m["kind"] == "baseline-fallback")
    metadata = match_metadata(p, m)
    o = m["learner_owner"]
    row = SimpleNamespace(owner=o, match_id=m["game_id"], behavior_checkpoint_sha256=p["current_sha256"],
                          deck=m["owner_decks"][o], opponent_deck=m["owner_decks"][1-o])
    shard = SimpleNamespace(manifest={"contract": dict(mixed_opponents=metadata, action_constraints=CONSTRAINTS)}, records=[row])
    validate_shard_ownership(shard, metadata)
    shard.records.append(SimpleNamespace(**{**vars(row), "owner": 1-o}))
    with pytest.raises(ValueError, match="frozen opponent"):
        validate_shard_ownership(shard, metadata)
    shard.records.pop()
    row.deck = list(reversed(row.deck))
    with pytest.raises(ValueError, match="deck mismatch"):
        validate_shard_ownership(shard, metadata)


def test_row_mass_and_per_deck_advantage_normalization():
    lanes = [SimpleNamespace(deck_id=d, time_steps=n) for d, n in zip(DECK_IDS, (2, 9, 4, 3))]
    weights = stage(1)[1]
    mass = objective_row_weights(lanes, weights, device="cpu")
    chunks = mass.split([l.time_steps for l in lanes])
    assert [float(c.sum()) for c in chunks] == pytest.approx(list(weights.values()))
    normalized = normalize_deck_advantages(torch.arange(18.).square(), lanes, weights)
    for chunk in normalized.split([l.time_steps for l in lanes]):
        assert float(chunk.mean()) == pytest.approx(0., abs=1e-6)
        assert float(chunk.std(unbiased=False)) == pytest.approx(1.)
    with pytest.raises(ValueError, match="every"):
        objective_row_weights(lanes[:-1], weights, device="cpu")


def mixed_lanes():
    torch.manual_seed(897)
    model = _event_model().eval()
    with torch.no_grad():
        lanes = [replace(_play_lane(model, n, rewards=[0.] * (n-1) + [r]), deck_id=d)
                 for d, n, r in zip(DECK_IDS, (5, 3, 2, 4), (1., -.5, .8, -1.))]
    return model, lanes


def test_weighted_packed_and_chunked_gradients_match_independent_deck_objective():
    model, lanes = mixed_lanes()
    weights = stage(1)[1]
    anchor = clone_model(model)
    with torch.no_grad():
        anchor.delay_head[-1].bias.add_(torch.linspace(-.4, .4, anchor.delay_head[-1].bias.numel()))
    expected_grads = None
    expected_kl = None
    for lane in lanes:
        candidate = clone_model(model)
        opt = RecordingSGD(candidate.parameters(), lr=0.)
        stats = event_ppo_update(candidate, [lane], opt, reference_model=anchor,
            config=EventPpoConfigV1(minibatch_lanes=0, max_grad_norm=1e6,
                kl_coefficient=.01 / weights["golem"] if lane.deck_id == "golem" else 0.))
        if lane.deck_id == "golem":
            expected_kl = stats.kl_reference
        weighted = [None if g is None else g * weights[lane.deck_id] for g in opt.gradients]
        if expected_grads is None:
            expected_grads = weighted
        else:
            expected_grads = [b if a is None else a if b is None else a+b for a, b in zip(expected_grads, weighted)]
    for chunk, packed in ((32, False), (2, False), (3, True)):
        candidate = clone_model(model)
        opt = RecordingSGD(candidate.parameters(), lr=0.)
        cache = PreparedEventRollout(candidate, lanes, row_batch_size=chunk) if packed else None
        stats = event_ppo_update(candidate, lanes, opt, reference_model=anchor, prepared=cache,
            config=EventPpoConfigV1(minibatch_lanes=0, sequence_chunk_steps=chunk, max_grad_norm=1e6,
                kl_coefficient=.01, deck_weights=weights, reference_deck_id="golem", target_kl=.01))
        assert stats.optimizer_steps == 1
        assert stats.ratio_mean == pytest.approx(1., abs=1e-5)
        assert stats.kl_reference == pytest.approx(expected_kl, abs=2e-6)
        for actual, expected in zip(opt.gradients, expected_grads):
            if expected is None:
                assert actual is None
            else:
                torch.testing.assert_close(actual, expected, atol=3e-5, rtol=3e-4)


def test_resume_checks_parent_chain_and_round_number(tmp_path):
    from tools.event_mixed_train import completed_rounds
    import hashlib
    weights = tmp_path / "weights"
    weights.mkdir()
    sha = hashlib.sha256(b"checkpoint").hexdigest()
    path = weights / (sha + ".pt")
    path.write_bytes(b"checkpoint")
    directory = tmp_path / "iteration-001251"
    directory.mkdir()
    record = dict(iteration=1251, mixed_round=1, parent_checkpoint_sha256=BASELINE["sha256"], checkpoint=str(path))
    (directory / "success.json").write_text(json.dumps(record))
    config = dict(start_iteration=1250, initial_checkpoint_sha256=BASELINE["sha256"])
    assert completed_rounds(tmp_path, config) == [record]
    record["mixed_round"] = 2
    (directory / "success.json").write_text(json.dumps(record))
    with pytest.raises(ValueError, match="contiguous"):
        completed_rounds(tmp_path, config)


@pytest.mark.parametrize("worker_host", ["34", "39"])
def test_cache_pruning_executes_only_after_archive_verification(tmp_path, worker_host):
    import hashlib
    import subprocess
    from tools.event_mixed_train import prune_inference_caches
    old_content = b"old-inference"
    old_artifact = hashlib.sha256(old_content).hexdigest()
    full_content = b"archived-checkpoint"
    full_sha = hashlib.sha256(full_content).hexdigest()
    archive = tmp_path / "archive"
    (archive / "weights").mkdir(parents=True)
    (archive / "weights" / (full_sha + ".pt")).write_bytes(full_content)
    worker = tmp_path / "worker"
    for folder in ("collector-weights", "history-inference"):
        (worker / folder).mkdir(parents=True)
        (worker / folder / (old_artifact + ".pt")).write_bytes(old_content)
    (tmp_path / "inference-assets.json").write_text(json.dumps({old_artifact: full_sha}))
    calls = []
    def ssh(host, command, **kwargs):
        calls.append(host)
        return subprocess.run(command, shell=True, check=True, **kwargs)
    fleet = SimpleNamespace(ROOT=tmp_path, FLEET={"37": dict(run=str(archive)), worker_host: dict(run=str(worker))}, ssh=ssh)
    descriptor = dict(artifact_sha256="1" * 64, source_checkpoint_sha256="2" * 64)
    collector = SimpleNamespace(descriptor=descriptor, histories={})
    learner = SimpleNamespace(descriptor=descriptor)
    prune_inference_caches(fleet, collector, learner)
    assert calls[0] == "37"
    assert not list(worker.glob("*/*.pt"))
    assert (archive / "weights" / (full_sha + ".pt")).read_bytes() == full_content
