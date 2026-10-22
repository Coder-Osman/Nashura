from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import tempfile

import pytest
import torch

from native_runner.tests.test_fair_search import SHA, frame, information, model
from native_runner.training.fair_search.config import SearchConfig
from native_runner.training.fair_search.inference import (
    BatchInference, LocalInference, SocketInferenceClient, SocketInferenceServer,
)


def compare(actual, expected):
    assert actual[0].keys() == expected[0].keys()
    assert actual[1] == pytest.approx(expected[1], abs=1e-6)
    for action in actual[0]:
        assert actual[0][action] == pytest.approx(expected[0][action], rel=2e-5, abs=1e-7)


def test_cached_sampling_and_scores_preserve_request_rng(model, information):
    config = SearchConfig(candidates=8, initial_candidates=4)
    reference = LocalInference(model, SHA, config=config, cache_entries=0, score_batch=1, fast_sampling=False)
    optimized = LocalInference(model, SHA, config=config)
    before = torch.get_rng_state().clone()
    for seed in (12, 31, 17, 12):
        assert optimized.sample(information, seed=seed) == reference.sample(information, seed=seed)
        compare(optimized.predict(information, seed=seed), reference.predict(information, seed=seed))
    assert torch.equal(before, torch.get_rng_state())
    assert optimized.profile["encoded_rows"] == 1
    assert optimized.profile["cache_hits"] == 7
    assert optimized.profile["score_hits"] > 0
    assert optimized.profile["score_batches"] < reference.profile["score_batches"]


def test_cache_key_includes_masks_owner_and_tensor_mutations(model, information):
    backend = LocalInference(model, SHA)
    backend.sample(information, seed=1)
    backend.sample(replace(information, owner=1), seed=1)
    backend.sample(replace(information, tick=information.tick + 1), seed=1)
    information.masks["cell_masks"][0, 0, 1] = False
    backend.sample(information, seed=1)
    assert backend.profile["encoded_rows"] == 4
    assert backend.profile["cache_hits"] == 0


def test_cache_is_bounded_and_frozen_model_is_checked_on_hit(model, information):
    backend = LocalInference(model, SHA, cache_entries=1)
    backend.sample(information, seed=1)
    first_size = backend.profile["cache_bytes"]
    backend.sample(replace(information, owner=1), seed=1)
    assert len(backend.cache) == 1 and backend.profile["evictions"] == 1
    assert backend.profile["cache_bytes"] == first_size
    with torch.no_grad():
        model.wdl_head.bias.add_(1)
    with pytest.raises(ValueError, match="changed"):
        backend.sample(replace(information, owner=1), seed=1)
    backend.clear_cache()
    assert not backend.cache and backend.profile["cache_bytes"] == 0


def test_oversized_context_not_cached(model, information):
    backend = LocalInference(model, SHA, cache_bytes=1)
    backend.sample(information, seed=1)
    backend.sample(information, seed=1)
    assert backend.profile["encoded_rows"] == 2
    assert not backend.cache and backend.profile["cache_bytes"] == 0


@pytest.mark.parametrize("change", ["buffer", "identity", "child_training"])
def test_cache_rejects_changed_model_identity_and_buffers(model, information, change):
    backend = LocalInference(model, SHA)
    backend.sample(information, seed=1)
    if change == "buffer":
        with torch.no_grad():
            next(model.buffers()).zero_()
    elif change == "identity":
        backend.identity = "b" * 64
    else:
        model.policy.train()
    with pytest.raises(ValueError, match="changed"):
        backend.sample(information, seed=1)


def test_batch_mixed_cache_hits_and_misses(model, information):
    backend = LocalInference(model, SHA)
    other = replace(information, owner=1)
    expected = backend.sample(information, seed=12)
    requests = [dict(operation="sample", information=info, seed=12) for info in (information, other)]
    replies = backend.infer_batch(requests)
    assert replies == [dict(action=expected)] * 2
    assert backend.profile["encoded_rows"] == 2
    assert backend.profile["cache_hits"] == 1


def test_fast_sampling_matches_reference_on_wait_and_play(model, information):
    reference = LocalInference(model, SHA, fast_sampling=False)
    optimized = LocalInference(model, SHA)
    actions = set()
    for seed in range(64):
        expected = reference.sample(information, seed=seed)
        assert optimized.sample(information, seed=seed) == expected
        actions.add(expected)
    assert "wait" in actions and len(actions) > 1
    information.masks["gate_mask"][0, 1] = False
    information.masks["card_mask"].zero_()
    information.masks["cell_masks"].zero_()
    information.masks["delay_mask"].zero_()
    compare(optimized.predict(information, seed=1), reference.predict(information, seed=1))


def test_real_socket_batch_preserves_candidates_and_rng(model, information):
    config = SearchConfig(candidates=8, initial_candidates=4, inference_wait_ms=20)
    reference = LocalInference(model, SHA, config=config, cache_entries=0, score_batch=1, fast_sampling=False)
    requests = [(replace(information, owner=index % 2), 1755 + index) for index in range(4)]
    expected = [reference.predict(info, seed=seed) for info, seed in requests]
    backend = LocalInference(model, SHA, config=config)
    batch = BatchInference({SHA: backend}, config=config)
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="fair-opt-") as directory:
        server = SocketInferenceServer(directory + "/service", batch)

        def request(item):
            info, seed = item
            client = SocketInferenceClient(server.path, SHA)
            try:
                return client.predict(info, seed=seed)
            finally:
                client.close()

        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                actual = list(pool.map(request, requests))
            for current, baseline in zip(actual, expected):
                compare(current, baseline)
            assert batch.profile["rows"] == 4
            assert batch.profile["max_batch"] > 1
        finally:
            server.close()


@pytest.mark.parametrize("settings", [{"cache_entries": -1}, {"cache_bytes": -1}, {"score_batch": 0},
                                     {"score_batch": 33}])
def test_invalid_inference_bounds(model, settings):
    with pytest.raises(ValueError, match="bounds"):
        LocalInference(model, SHA, **settings)
