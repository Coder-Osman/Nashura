"""Pure behavior and profiling tests; no Android Emulator or network required."""

from types import SimpleNamespace

import pytest
import torch

from native_runner.training.sspi.performance import (
    NativeRequestProfiler, NativeTickCounter, TimedSearchInference,
)
from native_runner.training.sspi.model import SSPIPolicy
from native_runner.training.sspi.protocol import ActionMode, SSPIAction, SSPILegality
from native_runner.training.sspi.search import SSPIJointPolicy
from native_runner.tests.test_fair_final_search import search_fixture


def test_sspi_candidate_topk_is_stable_with_ties_and_illegal_cells():
    class Frozen:
        legality = object()

        def __init__(self):
            self.scores = None

        def conditionals(self, prefix=()):
            assert prefix == (ActionMode.PLAY, 0)
            return self.scores

    frozen = Frozen()
    adapter = SSPIJointPolicy(frozen)
    generator = torch.Generator().manual_seed(123)
    for _ in range(20):
        scores = torch.randint(-5, 6, (576,), generator=generator).float()
        scores[torch.randperm(576, generator=generator)[:35]] = -torch.inf
        frozen.scores = scores
        indices = torch.where(torch.isfinite(scores))[0].tolist()
        for count in (1, 2, 8, 40, 900):
            reference = sorted(indices, key=lambda index: (-float(scores[index]), index))[:count]
            assert adapter.topk_conditionals((1, 0), count) == reference
    with pytest.raises(ValueError, match="positive"):
        adapter.topk_conditionals((1, 0), 0)


def test_sspi_conditional_cache_reuses_exact_tensors_and_keeps_autograd():
    plays = torch.zeros((1, 576, 5), dtype=torch.bool)
    plays[0, 11, 0] = True
    legality = SSPILegality((123,), plays)
    mode_logits = torch.nn.Parameter(torch.tensor([0.2, -0.5, 4.0]))
    calls = []

    def logits(prefix):
        calls.append(tuple(prefix))
        if not prefix:
            return mode_logits
        if prefix == (ActionMode.PLAY,):
            return torch.zeros(1)
        if prefix == (ActionMode.PLAY, 0):
            return torch.zeros(576)
        return torch.zeros(5)

    policy = SSPIPolicy(logits, legality)
    first = policy.conditionals()
    assert first is policy.conditionals()
    assert torch.equal(first, policy.conditionals(()))
    assert calls.count(()) == 1
    loss = -policy.log_prob(SSPIAction.wait())
    loss.backward()
    assert mode_logits.grad is not None
    assert torch.isfinite(mode_logits.grad).all()
    assert mode_logits.grad[0] < 0
    assert mode_logits.grad[1] > 0
    assert mode_logits.grad[2] == 0


def test_probe_native_request_profile_distinguishes_step_requests_from_ticks():
    profile = NativeRequestProfiler()
    profile.record("step 3", 0.1, True)
    profile.record("step 8", 0.2, False)
    profile.record("observe-atomic", 0.5, True)
    profile.record("restore 7", 0.03, True)
    summary = profile.summary()
    assert summary["successful_step_ticks_requested"] == 3
    assert summary["commands"]["step"]["calls"] == 2
    assert summary["commands"]["step"]["successes"] == 1
    assert summary["commands"]["observe-atomic"]["seconds"] == pytest.approx(0.5)
    assert "not observed tick deltas" in summary["tick_semantics"]


def test_probe_exact_tick_counter_never_counts_restores():
    counter = NativeTickCounter()
    clock = SimpleNamespace(tick=10)
    rollout = SimpleNamespace(runtime=SimpleNamespace(clock=clock))

    def advance_two_ticks():
        clock.tick += 2
        return "advanced"

    assert counter.advance(rollout, advance_two_ticks) == "advanced"
    clock.tick = 10  # snapshot restore should not count as physical advancement
    assert counter.advance(rollout, advance_two_ticks) == "advanced"
    assert counter.summary()["actual_ticks"] == 4
    assert counter.summary()["advance_calls"] == 2
    with pytest.raises(ValueError, match="did not progress"):
        counter.advance(rollout, lambda: "nothing")
    assert counter.actual_ticks == 4


def test_probe_opponent_sampling_times_do_not_change_actions_or_rng():
    class Frozen:
        checkpoint_sha = "a" * 64
        controller_sha = "b" * 64

        def predict(self, info):
            return info, 7

        def sample(self, info, generator):
            return int(torch.randint(0, 10, (), generator=generator))

    timed = TimedSearchInference(Frozen())
    assert timed.predict("x") == ("x", 7)
    seed = 42
    expected = Frozen().sample(None, torch.Generator().manual_seed(seed))
    actual = timed.sample(None, torch.Generator().manual_seed(seed))
    assert actual == expected
    assert timed.predict_calls == 1
    assert timed.opponent_sample_calls == 1
    assert timed.summary()["opponent_sample_seconds"] >= 0.0


def test_completed_search_audit_separately_times_candidate_generation():
    search, info, args = search_fixture()
    result = search.run(info, **args)
    assert result.result["valid_simulations"] == 128
    assert "candidate_generation" in result.audit["timings"]
    assert "tree_selection" in result.audit["timings"]
    assert result.audit["timings"]["candidate_generation"] >= 0.0
    assert result.audit["timings"]["tree_selection"] >= 0.0
