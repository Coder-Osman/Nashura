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
from native_runner.training.sspi.search_probe import LegacySearchNative, run as run_search_probe
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


@pytest.mark.parametrize("transport", ("legacy", "persistent"))
def test_search_probe_accepts_only_explicit_known_transports(transport):
    with pytest.raises(ValueError, match="invalid independent ports"):
        run_search_probe(1, 1, simulations=128, transport=transport)


def test_search_probe_defaults_to_persistent_root_and_verified_delta_belief():
    import inspect
    from native_runner.training.sspi.search_probe import run

    parameters = inspect.signature(run).parameters
    assert parameters["transport"].default == "persistent"
    assert parameters["belief_transport"].default == "persistent-delta"


def test_transition_phase_profile_partitions_existing_calls_without_changing_result():
    from native_runner.training.sspi.search_probe import _attach_transition_profile

    class TickCounter:
        advance_seconds = 0.0

    class Inference:
        opponent_sample_seconds = 0.0

    class Rollout:
        advance_phase_seconds = {}

    class World:
        def __init__(self):
            self.rollout = Rollout()
            self.calls = []

        def _commit(self, opponent, generator, action=None):
            self.calls.append("commit")
            opponent.opponent_sample_seconds += 0.001

        def root_owner_information(self):
            self.calls.append("information")
            return "root"

        def transition(self, action, *, opponent, generator, hard_tick, maximum_decisions, check_deadline):
            self._commit(opponent, generator, action)
            self.rollout.advance_phase_seconds["step_native_bind_seconds"] = 0.002
            counter.advance_seconds += 0.003
            self.root_owner_information()
            return "transition-result"

    counter, inference, world, profile = TickCounter(), Inference(), World(), {}
    _attach_transition_profile(world, profile, tick_counter=counter, inference=inference)
    result = world.transition("action", opponent=inference, generator=None, hard_tick=10,
        maximum_decisions=2, check_deadline=lambda: None)

    assert result == "transition-result"
    assert world.calls == ["commit", "information"]
    accounted = sum(profile[name] for name in (
        "opponent_sample_seconds", "action_commit_excluding_opponent_seconds",
        "advance_seconds", "boundary_information_seconds", "transition_other_seconds"))
    assert accounted == pytest.approx(profile["transition_seconds"], abs=1e-9)
    assert profile["advance_phases"]["step_native_bind_seconds"] == pytest.approx(0.002)
    assert profile["transition_calls"] == 1


def test_search_probe_rejects_unknown_transport_before_native_io():
    with pytest.raises(ValueError, match="invalid independent ports"):
        run_search_probe(1, 1, simulations=128, transport="batch")


def test_search_probe_legacy_native_accepts_restore_tick_rebasing():
    native = LegacySearchNative("127.0.0.1", 26789)
    assert native.start_atomic_steps(initial_tick=90) is None


def test_search_probe_rejects_unknown_belief_transport_before_native_io():
    with pytest.raises(ValueError, match="invalid independent ports"):
        run_search_probe(1, 1, simulations=128, transport="persistent", belief_transport="batch")


def test_search_probe_cleanup_stops_only_its_exclusive_resident_slot():
    from native_runner.training.sspi.search_probe import cleanup_owned_event_worker

    class Native:
        env_id = 0

        def __init__(self, slots):
            self.slots = slots
            self.commands = []

        def close_transport(self):
            self.commands.append("close")

        def resident_status(self):
            return {"mode": "resident-headless", "occupied": len(self.slots), "slots": self.slots}

        def _raw_request(self, command):
            self.commands.append(command)

    owned = Native([{"envId": 0}])
    foreign = Native([{"envId": 0}, {"envId": 1}])

    cleanup_owned_event_worker(owned)
    cleanup_owned_event_worker(foreign)
    assert owned.commands == ["close", "multi-stop"]
    assert foreign.commands == ["close"]


def _real_information():
    from dataclasses import replace
    from native_runner.contracts import frozen_mapping
    from native_runner.tests.test_fair_final_history import opening_fixture
    from native_runner.tests.test_sspi_temporal_augmentation import information
    from native_runner.training.fair_final.information import build_fair_tensorizer

    observation, _ = opening_fixture()
    observation = replace(observation, episode_id="sspi-cache-test")
    player = observation.players[0]
    metadata = dict(player.metadata)
    metadata["hand_runtime_by_slot"] = {str(index): {"form_code": 0} for index in range(4)}
    player = replace(player, metadata=frozen_mapping(metadata))
    mask = replace(observation.action_mask, reasons=frozen_mapping(
        {**dict(observation.action_mask.reasons), "effective_elixir": 5.}))
    observation = replace(observation, players=(player, observation.players[1]), action_mask=mask)
    tensorizer = build_fair_tensorizer(observation)
    tensorizer.start_episode(observation, initial_elixir={0: 5., 1: 5.})
    return replace(information(), frame=tensorizer.tensorize(observation))


def test_static_cache_preserves_policy_wdl_gradients_adamw_and_checkpoint(tmp_path):
    from native_runner.training.sspi.model import initialize_fresh_model
    from native_runner.training.sspi.learning import (
        TrainingRow, WDLLabel, search_root_loss, create_optimizer, save_checkpoint, load_checkpoint,
    )

    torch.set_num_threads(1)
    info = _real_information()
    cached = initialize_fresh_model(20261103).eval()
    reference = initialize_fresh_model(20261103).eval()
    reference.load_state_dict(cached.state_dict())
    reference.encoder.cache_static_inference = False
    before = {name: value.clone() for name, value in cached.state_dict().items()}
    with torch.inference_mode():
        for _ in range(2):
            actual, value = cached(info, [info.legality])
            expected, expected_value = reference(info, [info.legality])
            assert torch.equal(value, expected_value)
            for prefix in ((), (ActionMode.PLAY,), (ActionMode.PLAY, 0), (ActionMode.PLAY, 0, 17)):
                assert torch.equal(actual[0].conditionals(prefix), expected[0].conditionals(prefix))
            for card in torch.where(info.legality.cards)[0].tolist():
                prefix = (ActionMode.PLAY, card)
                assert torch.equal(actual[0].conditionals(prefix), expected[0].conditionals(prefix))
                for cell in torch.where(info.legality.cells[card])[0].tolist():
                    assert torch.equal(actual[0].conditionals((*prefix, cell)),
                        expected[0].conditionals((*prefix, cell)))
            a = torch.Generator().manual_seed(90)
            b = torch.Generator().manual_seed(90)
            assert actual[0].sample(a)[0] == expected[0].sample(b)[0]
            assert torch.equal(a.get_state(), b.get_state())
    assert cached.encoder._static_memory_cache is not None
    assert all(torch.equal(value, before[name]) for name, value in cached.state_dict().items())
    row = TrainingRow(info, info.legality, WDLLabel.WIN, "SEARCH",
        ((SSPIAction.wait(), 32), (SSPIAction.play(info.hand.ready_card_ids[0], 17, 2), 96)), 128)
    optimizers = [create_optimizer(model) for model in (cached, reference)]
    # Eval with gradients also bypasses the cache, even without train().
    for _ in range(2):
        for model, optimizer in zip((cached, reference), optimizers):
            optimizer.zero_grad(set_to_none=True)
            losses = search_root_loss(model, [row])
            (losses["policy_loss"] + losses["value_loss"]).backward()
        assert cached.encoder._static_memory_cache is None
        nonzero_static = False
        for (name, actual), (_, expected) in zip(cached.named_parameters(), reference.named_parameters()):
            assert (actual.grad is None) == (expected.grad is None), name
            if actual.grad is not None:
                assert torch.equal(actual.grad, expected.grad), name
                if "mechanic_profile_encoder" in name and bool(actual.grad.any()):
                    nonzero_static = True
        assert nonzero_static
        for optimizer in optimizers:
            optimizer.step()
        assert all(torch.equal(value, reference.state_dict()[name])
            for name, value in cached.state_dict().items())
        with torch.inference_mode():
            assert torch.equal(cached(info, [info.legality])[1], reference(info, [info.legality])[1])
    path = tmp_path / "checkpoint.pt"
    identity = dict(run="static-cache-test")
    for optimizer in optimizers:
        optimizer.zero_grad(set_to_none=True)
    saved = save_checkpoint(path, cached, optimizers[0], identity=identity, optimizer_steps=2)
    restored = initialize_fresh_model(20261103).eval()
    restored_optimizer = create_optimizer(restored)
    with torch.inference_mode():
        restored(info, [info.legality])
    loaded = load_checkpoint(path, restored, restored_optimizer, expected_identity=identity)
    assert loaded["model_sha"] == saved["model_sha"]
    assert restored.encoder._static_memory_cache is None
    with torch.inference_mode():
        assert torch.equal(cached(info, [info.legality])[1], restored(info, [info.legality])[1])
    from native_runner.training.fair_search.contracts import semantic_digest
    assert semantic_digest(restored_optimizer.state_dict()) == semantic_digest(optimizers[0].state_dict())


@pytest.mark.parametrize("change", ["parameter", "buffer", "replacement", "load", "train", "dtype"])
def test_static_cache_invalidates_on_model_changes(change):
    from native_runner.training.sspi.model import initialize_fresh_model

    torch.set_num_threads(1)
    model = initialize_fresh_model(20261103).eval()
    encoder = model.encoder
    with torch.inference_mode():
        first = encoder._inference_static_memories()
        assert encoder._inference_static_memories() is first
    with torch.no_grad():
        if change == "parameter":
            encoder.mechanic_profile_encoder.numeric_encoder[0].weight.add_(0.02)
        elif change == "buffer":
            encoder.effect_encoder.catalog_features.add_(0.01)
        elif change == "replacement":
            encoder.effect_encoder.catalog_features = encoder.effect_encoder.catalog_features.clone() + 0.01
        elif change == "load":
            model.load_state_dict(model.state_dict())
        elif change == "train":
            model.train()
            assert encoder._static_memory_cache is None
            model.eval()
        else:
            model.double()
    with torch.inference_mode():
        second = encoder._inference_static_memories()
        assert second is not first
        effects = encoder.effect_encoder.all_effects()
        profiles = encoder.mechanic_profile_encoder(effects)
        assert torch.equal(second[0], effects)
        assert torch.equal(second[1], profiles)


def test_static_cache_requires_eval_inference_and_versioned_inputs():
    from native_runner.training.sspi.model import initialize_fresh_model

    model = initialize_fresh_model(20261103)
    with torch.inference_mode():
        assert model.encoder._inference_static_memories() is None
    model.eval()
    with torch.no_grad():
        assert model.encoder._inference_static_memories() is None
    with torch.inference_mode(), torch.autocast("cpu", dtype=torch.bfloat16):
        assert model.encoder._inference_static_memories() is None
    with torch.inference_mode():
        assert model.encoder._inference_static_memories() is not None
        model.encoder.effect_encoder.catalog_features = model.encoder.effect_encoder.catalog_features.clone()
        assert model.encoder._inference_static_memories() is None


def test_sspi_information_fast_digest_matches_legacy_and_rereads_mutable_inputs():
    from native_runner.training.fair_search.contracts import semantic_digest

    info = _real_information()
    def legacy_digest():
        return semantic_digest(dict(schema="sspi-fair-information.v2", owner=info.owner,
            tick=info.tick, frame=info.frame, hand=info.hand, legality=info.legality.identity()))

    expected = legacy_digest()
    assert info.sha256 == expected
    assert info.sha256 == expected
    info.legality.plays[0, 0, 0] = ~info.legality.plays[0, 0, 0]
    assert info.sha256 == legacy_digest()
    assert info.sha256 != expected
    # NumPy writes do not advance PyTorch's mutation counter: hash actual bytes.
    previous = info.sha256
    info.frame.match_scalars.numpy()[0, 0] += 1
    assert info.sha256 == legacy_digest() and info.sha256 != previous
    previous = info.sha256
    info.legality.pending_arrival_ticks[info.legality.card_ids[0]] = 4
    assert info.sha256 == legacy_digest() and info.sha256 != previous


@pytest.mark.parametrize("shape", [(4, 576, 5), (2, 3, 7), (2, 3, 0), (2, 0, 5), (0, 4, 5), (2, 3, 16)])
def test_boolean_tensor_list_digest_matches_nested_python_lists(shape):
    from native_runner.training.fair_search.contracts import BooleanTensorList, semantic_digest

    tensor = torch.randint(0, 2, shape, dtype=torch.bool)
    assert semantic_digest(BooleanTensorList(tensor)) == semantic_digest(tensor.tolist())


def test_boolean_digest_noncontiguous_all_patterns_and_mutation():
    from native_runner.training.fair_search.contracts import BooleanTensorList, semantic_digest

    tensor = ((torch.arange(32)[:, None] >> torch.arange(5)) & 1).bool()
    tensor = tensor[None].expand(3, -1, -1).transpose(0, 1)
    assert not tensor.is_contiguous()
    view = BooleanTensorList(tensor)
    assert semantic_digest(dict(mask=view)) == semantic_digest(dict(mask=tensor.tolist()))
    previous = semantic_digest(view)
    tensor[0, 0, 0] = ~tensor[0, 0, 0]
    assert semantic_digest(view) == semantic_digest(tensor.tolist()) != previous
