from copy import deepcopy
import json

import pytest
import torch
from torch import nn

from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.sspi.learning import (
    SEARCH_BUDGETS,
    TrainingRow,
    WDLLabel,
    create_optimizer,
    load_checkpoint,
    save_checkpoint,
    search_root_loss,
    train_search_step,
)
from native_runner.training.sspi.model import HAND_FEATURE_DIM, SSPIPolicy, _hand_features, initialize_fresh_model
from native_runner.training.sspi.protocol import (
    ABILITY_FEATURE_DIM,
    AbilityCandidate,
    ActionMode,
    HandState,
    SSPILegality,
    SSPIAction,
    SSPIInformation,
    from_full_action,
    to_full_action,
)
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.sspi.collector import (
    ARM_CONFIG,
    SearchNative,
    _native_execution_action,
    decision_gate,
    episode_seed_identity,
)
from native_runner.training.sspi.experiment import (
    _generation_episode_id,
    _restore_generation_checkpoint,
    evaluate_generation,
    exposure_batches,
    paired_scores_from_results,
    paired_score_interval,
    run_next_generation,
)
from native_runner.training.sspi.search import SSPIJointPolicy, budget_search_sha
from native_runner.training.fair_final.candidates import structured_candidates
from native_runner.training.fair_final.search import SearchLimits


def native_player(holes=()):
    deck = [{"deckSlot": slot, "cardId": card} for slot, card in enumerate(HOG_DECK)]
    hand = [{**deck[index], "handIndex": index} for index in range(4) if index not in holes]
    cycle_cards = [deck[index] for index in holes] + deck[4:]
    cycle = [{**card, "cycleIndex": index} for index, card in enumerate(cycle_cards)]
    next_card = cycle[0] if cycle else None
    return {"deck": deck, "hand": hand, "cycle": cycle,
        "nextCard": None if next_card is None else dict(next_card)}


def refill_one(player):
    result = deepcopy(player)
    holes = sorted(set(range(4)) - {item["handIndex"] for item in result["hand"]})
    card = result["cycle"].pop(0)
    result["hand"].append({key: card[key] for key in ("deckSlot", "cardId")} | {"handIndex": holes[0]})
    result["cycle"] = [{key: item[key] for key in ("deckSlot", "cardId")} | {"cycleIndex": index}
        for index, item in enumerate(result["cycle"])]
    result["nextCard"] = None if not result["cycle"] else dict(result["cycle"][0])
    return result


def permute_native_deck_slots(player):
    result = deepcopy(player)
    slot_map = {slot: (slot + 3) % 8 for slot in range(8)}
    for zone in ("deck", "hand", "cycle"):
        for item in result[zone]:
            item["deckSlot"] = slot_map[item["deckSlot"]]
    if result["nextCard"] is not None:
        result["nextCard"]["deckSlot"] = slot_map[result["nextCard"]["deckSlot"]]
    return result


@pytest.mark.parametrize("holes,ready", [((1,), 3), ((0, 2), 2), ((0, 1, 3), 1)])
def test_native_hand_slots_keep_known_pending_cards_unplayable_and_restore_stably(holes, ready):
    player = native_player(holes)
    first = HandState.from_native(player)
    restored = HandState.from_native(deepcopy(player))
    assert first == restored
    assert first.ready_hand_count == ready
    assert first.known_next_card_id == player["nextCard"]["cardId"]
    assert first.next_card_available_now is False
    assert len(first.pending_refills) == len(holes)
    assert all(not refill.time_known and refill.remaining_ticks is None for refill in first.pending_refills)
    assert [slot.index for slot in first.slots if slot.refill_pending] == list(holes)
    assert first.known_next_card_id not in first.ready_card_ids

    one_refill = HandState.from_native(refill_one(player))
    assert one_refill.ready_hand_count == ready + 1
    assert len(one_refill.pending_refills) == len(holes) - 1
    assert set(first.deck) == set(one_refill.deck)


def test_hand_cycle_rejects_missing_identity_and_non_native_integer_indices():
    missing = native_player((0, 2))
    missing["cycle"].pop()
    with pytest.raises(ValueError, match="refill queue"):
        HandState.from_native(missing)
    malformed = native_player()
    malformed["hand"][0]["handIndex"] = True
    with pytest.raises(ValueError, match="handIndex"):
        HandState.from_native(malformed)


def test_hand_features_include_physical_slots_fifo_and_known_unavailable_next_card():
    hand = HandState.from_native(native_player((0, 2)))
    features = _hand_features(hand, device="cpu")
    assert features.shape == (HAND_FEATURE_DIM,)
    assert features[0] == 0. and features[9] == 1.
    assert features[20] == 1.
    assert features[-1] == 0.
    refilled = HandState.from_native(refill_one(native_player((0, 2))))
    assert _hand_features(refilled, device="cpu").shape == features.shape


def test_stable_card_identity_ignores_random_native_deck_slot_permutation():
    player = native_player((0, 2))
    original = HandState.from_native(player)
    permuted = HandState.from_native(permute_native_deck_slots(player))

    assert permuted == original
    assert torch.equal(_hand_features(permuted, device="cpu"), _hand_features(original, device="cpu"))


def test_frozen_sspi_information_preserves_physical_hand_and_refill_queue():
    hand = HandState.from_native(native_player((0, 2)))
    legality = SSPILegality((), torch.empty((0, 576, 5), dtype=torch.bool))
    information = SSPIInformation(0, 100, {"fair": "frame"}, hand, legality)
    rollout = type("RolloutStub", (), {
        "runtime": type("RuntimeStub", (), {"freeze_due_information": lambda self: {}})(),
        "information": {0: information},
    })()

    frozen = FinalRollout.freeze_due_information(rollout)[0]

    assert frozen.hand == hand
    assert frozen.hand.ready_hand_count == 2
    assert frozen.hand.known_next_card_id == hand.known_next_card_id
    assert frozen.hand.pending_refills == hand.pending_refills


def test_wait_only_root_masks_play_and_ability_and_always_samples_wait():
    legality = SSPILegality((), torch.empty((0, 576, 5), dtype=torch.bool))
    assert legality.mask().tolist() == [True, False, False]
    assert legality.mask((ActionMode.PLAY,)).numel() == 0
    assert legality.ability_candidates == ()
    policy = SSPIPolicy(lambda prefix: torch.tensor([-5., 20., 30.]), legality)
    assert policy.conditionals().exp().tolist() == [1., 0., 0.]
    for seed in range(16):
        action, _ = policy.sample(torch.Generator().manual_seed(seed))
        assert action == SSPIAction.wait()


def test_ability_pointer_and_tagged_action_roundtrip_are_versionable():
    first = AbilityCandidate(101, 7, tuple([0.] * ABILITY_FEATURE_DIM))
    second = AbilityCandidate(202, 9, tuple([0.25] * ABILITY_FEATURE_DIM))
    legality = SSPILegality((), torch.empty((0, 576, 5), dtype=torch.bool),
        ability_candidates=(first, second))
    policy = SSPIPolicy(lambda prefix: torch.tensor([0., 0., 1.]) if not prefix
        else torch.tensor([0., 2.]), legality)
    action = SSPIAction.ability(202, 9)
    assert legality.legal(action)
    assert action.key == "ability:202:9"
    assert policy.log_prob(action).item() == pytest.approx(
        policy.conditionals()[ActionMode.ABILITY].item() + policy.conditionals((ActionMode.ABILITY,))[1].item())

    for delay_index, delay_slot in enumerate((2, 4, 6, 8, 10)):
        action = SSPIAction.play(HOG_DECK[0], 17, delay_index)
        assert from_full_action(to_full_action(action)) == action
        assert to_full_action(action).delay_slot == delay_slot


def test_collector_transport_is_explicit_and_legacy_remains_default():
    legacy = SearchNative("127.0.0.1", 26789)
    persistent = SearchNative("127.0.0.1", 26789, transport="persistent")
    assert legacy._persistent_transport_requested is False
    assert persistent._persistent_transport_requested is True
    with pytest.raises(ValueError, match="transport"):
        SearchNative("127.0.0.1", 26789, transport="unknown")
    legacy.close_transport()
    persistent.close_transport()


def test_sspi_training_has_only_the_5_percent_128_configuration():
    assert SEARCH_BUDGETS == {"A_5PCT_128": 128}
    assert ARM_CONFIG == {"A_5PCT_128": (0.05, 128)}


def test_collector_accepts_direct_full_actions_and_normalizes_tagged_actions():
    tagged = SSPIAction.play(HOG_DECK[0], 17, 3)
    native_action = to_full_action(tagged)
    assert isinstance(_native_execution_action(tagged, mode="DIRECT"), FullAction)
    assert _native_execution_action(native_action, mode="DIRECT") is native_action
    assert _native_execution_action(native_action, mode="SEARCH") is native_action
    with pytest.raises(TypeError, match="incompatible action type"):
        _native_execution_action(tagged, mode="SEARCH")


def test_sspi_policy_projects_only_five_legal_delays_into_puct_candidates():
    legality = one_play_legality()

    def logits(prefix):
        if not prefix:
            return torch.tensor([0., 1., 9.])
        if prefix == (ActionMode.PLAY,):
            return torch.zeros(1)
        if len(prefix) == 2:
            return torch.zeros(576)
        return torch.tensor([0., 1., 2., 3., 4.])

    base = SSPIPolicy(logits, legality)
    adapter = SSPIJointPolicy(base)
    assert adapter.prefix_mask().tolist() == [True, True]
    assert torch.where(adapter.prefix_mask((1, 0, 17)))[0].tolist() == [6]
    candidates = structured_candidates(adapter, seed=4, maximum=8)
    assert {entry.action.delay_slot for entry in candidates if entry.action.card_id is not None} == {6}
    assert sum(entry.prior for entry in candidates) == pytest.approx(1.)
    assert adapter.sample(torch.Generator().manual_seed(3))[0].delay_slot in (None, 6)


def test_sspi_search_budget_runs_all_128_effective_simulations():
    from native_runner.tests.test_fair_final_search import search_fixture

    limits = SearchLimits(root_noise=.25, root_noise_mode="training")
    search, info, arguments = search_fixture(limits=limits)
    arguments["identity"]["search_protocol_sha"] = budget_search_sha(limits, 128)
    outcome = search.run(info, simulation_budget=128, **arguments)
    assert outcome.result["valid_simulations"] == 128
    assert sum(entry["visits"] for entry in outcome.result["candidates"]) == 128
    assert sum(count for _, count in outcome.policy_target) == 128
    assert outcome.result["search_protocol_sha"] == budget_search_sha(limits, 128)
    assert search.belief.world.closed


def test_search_budget_is_fixed_to_128():
    limits = SearchLimits(root_noise=.25, root_noise_mode="training")
    assert budget_search_sha(limits, 128)
    for budget in (256, 1024):
        with pytest.raises(ValueError, match="128"):
            budget_search_sha(limits, budget)


class TinyPolicyValue(nn.Module):
    def __init__(self):
        super().__init__()
        self.mode_logits = nn.Parameter(torch.tensor([0.1, -0.2, 0.3]))
        self.wdl_logits = nn.Parameter(torch.tensor([0.2, -0.1, 0.05]))

    def forward(self, frame, legalities):
        if frame == "DIRECT_SENTINEL":
            raise AssertionError("DIRECT rows must be filtered before model evaluation")
        policies = []
        for legality in legalities:
            def logits(prefix):
                if not prefix:
                    return self.mode_logits
                if prefix == (ActionMode.PLAY,):
                    return self.mode_logits.new_zeros((len(legality.card_ids),))
                if len(prefix) == 2:
                    return self.mode_logits.new_zeros((576,))
                return self.mode_logits.new_zeros((5,))
            policies.append(SSPIPolicy(logits, legality))
        return policies, self.wdl_logits.unsqueeze(0)


def one_play_legality():
    plays = torch.zeros((1, 576, 5), dtype=torch.bool)
    plays[0, 17, 2] = True
    return SSPILegality((HOG_DECK[0],), plays)


def test_direct_rows_contribute_to_neither_loss_or_gradient():
    legality = one_play_legality()
    search = TrainingRow("SEARCH", legality, WDLLabel.WIN, "SEARCH",
        ((SSPIAction.play(HOG_DECK[0], 17, 2), 128),), 128)
    direct = TrainingRow("DIRECT_SENTINEL", legality, WDLLabel.LOSS, "DIRECT")
    model = TinyPolicyValue()
    mixed = search_root_loss(model, [search, direct])
    alone = search_root_loss(model, [search])
    assert mixed["search_rows"] == 1 and mixed["direct_rows"] == 1
    assert torch.equal(mixed["policy_loss"], alone["policy_loss"])
    assert torch.equal(mixed["value_loss"], alone["value_loss"])
    before = model.mode_logits.detach().clone()
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    result = train_search_step(model, optimizer, [search, direct])
    assert result["gradient_norm"] > 0 and result["optimizer_steps"] == 1
    assert not torch.equal(before, model.mode_logits.detach())


def test_checkpoint_restores_fresh_model_and_optimizer_state(tmp_path):
    model = initialize_fresh_model(20261101)
    optimizer = create_optimizer(model)
    parameter = next(model.parameters())
    parameter.square().sum().backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    expected = {name: value.detach().clone() for name, value in model.state_dict().items()}
    identity = {"run": "unit-test", "seed": 20261101, "action_schema": "sspi-tagged-action.v1"}
    path = tmp_path / "sspi.pt"
    saved = save_checkpoint(path, model, optimizer, identity=identity, optimizer_steps=1)

    with torch.no_grad():
        next(model.parameters()).add_(1.)
    restored = initialize_fresh_model(20261101)
    restored_optimizer = create_optimizer(restored)
    loaded = load_checkpoint(path, restored, restored_optimizer, expected_identity=identity)
    assert loaded["model_sha"] == saved["model_sha"]
    assert loaded["optimizer_steps"] == 1
    assert all(torch.equal(value, expected[name]) for name, value in restored.state_dict().items())
    assert restored_optimizer.state


def test_continuation_generation_identity_and_parent_checkpoint_restore(tmp_path):
    assert _generation_episode_id("A", 2, 1) == "A-g0002-game-0001"
    assert _generation_episode_id("V", 12, 4) == "V-g0012-game-0004"
    with pytest.raises(ValueError, match="identity"):
        _generation_episode_id("E", 2, 1)

    model = initialize_fresh_model(20261101)
    optimizer = create_optimizer(model)
    next(model.parameters()).square().sum().backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    checkpoint_path = tmp_path / "checkpoints" / "generation-0001.pt"
    identity = dict(run_id="continuation-test", generation=1, seed=20261101,
        architecture_sha=model.architecture_sha)
    saved = save_checkpoint(checkpoint_path, model, optimizer, identity=identity,
        optimizer_steps=1)
    record = dict(schema="sspi-mac-pilot-generation.v1", run_id="continuation-test",
        generation=1, theta0_sha256="theta0", checkpoint=saved)
    (tmp_path / "generation-0001.json").write_text(json.dumps(record))

    restored, restored_optimizer, parent, metadata = _restore_generation_checkpoint(
        tmp_path, 1, "continuation-test", bundle=None)

    assert parent["theta0_sha256"] == "theta0"
    assert metadata["model_sha"] == saved["model_sha"]
    assert metadata["optimizer_steps"] == 1
    assert restored_optimizer.state
    assert all(torch.equal(value, model.state_dict()[name])
        for name, value in restored.state_dict().items())


def test_continuation_parent_checkpoint_rejects_mismatched_record_checksum(tmp_path):
    model = initialize_fresh_model(20261101)
    optimizer = create_optimizer(model)
    checkpoint_path = tmp_path / "checkpoints" / "generation-0001.pt"
    saved = save_checkpoint(checkpoint_path, model, optimizer,
        identity=dict(run_id="continuation-test", generation=1, seed=20261101,
            architecture_sha=model.architecture_sha), optimizer_steps=0)
    saved["file_sha"] = "0" * 64
    record = dict(schema="sspi-mac-pilot-generation.v1", run_id="continuation-test",
        generation=1, checkpoint=saved)
    (tmp_path / "generation-0001.json").write_text(json.dumps(record))

    with pytest.raises(ValueError, match="checkpoint identity"):
        _restore_generation_checkpoint(tmp_path, 1, "continuation-test", bundle=None)


def test_next_generation_freezes_parent_then_trains_and_restores_checkpoint(monkeypatch, tmp_path):
    import native_runner.training.sspi.experiment as experiment
    from native_runner.training.fair_search.contracts import semantic_digest

    seed = 20261101
    run_id = "continuation-run"
    theta0_sha = semantic_digest(initialize_fresh_model(seed).state_dict())
    parent = initialize_fresh_model(seed)
    optimizer = create_optimizer(parent)
    parameter = next(parent.parameters())
    parameter.square().sum().backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    parent_sha = semantic_digest(parent.state_dict())
    identity = dict(run_id=run_id, generation=1, seed=seed,
        architecture_sha=parent.architecture_sha)
    checkpoint_path = tmp_path / "checkpoints" / "generation-0001.pt"
    checkpoint = save_checkpoint(checkpoint_path, parent, optimizer,
        identity=identity, optimizer_steps=1)
    (tmp_path / "generation-0001.json").write_text(json.dumps(dict(
        schema="sspi-mac-pilot-generation.v1", run_id=run_id, generation=1,
        theta0_sha256=theta0_sha, checkpoint=checkpoint)))
    (tmp_path / "run_config.json").write_text(json.dumps(dict(
        schema="sspi-mac-pilot-run.v1", run_id=run_id, arm="A_5PCT_128",
        device="cpu", seed=seed, transport="persistent", deck=list(HOG_DECK))))
    (tmp_path / "postgame_status.json").write_text(json.dumps(dict(
        status="complete", generation=1)))

    collection_models = []

    def collect(**kwargs):
        model = kwargs["model"]
        assert all(not item.requires_grad for item in model.parameters())
        assert semantic_digest(model.state_dict()) == parent_sha
        collection_models.append(model)
        destination = kwargs["run_dir"] / "episodes" / kwargs["game_id"]
        destination.mkdir(parents=True)
        kind = kwargs["episode_kind"]
        metadata = dict(schema="sspi-mac-pilot-episode.v1", status="complete",
            episode_kind=kind, training_admissible=(kind == "training"),
            search_probability=0.05, identity=dict(run_id=run_id, arm="A_5PCT_128",
                seed=seed, device="cpu", source_sha256="source",
                model_sha256_by_owner={"0": parent_sha, "1": parent_sha}))
        (destination / "episode.json").write_text(json.dumps(metadata))
        return destination, metadata

    def train_step(model, optim, rows):
        optim.zero_grad(set_to_none=True)
        next(model.parameters()).grad = torch.ones_like(next(model.parameters()))
        norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
        optim.step()
        optim.zero_grad(set_to_none=True)
        return dict(loss=1., policy_loss=.5, value_loss=.5, gradient_norm=float(norm),
            optimizer_steps=1, search_rows=128, direct_rows=0, microbatch=4,
            accumulation_steps=32, continuation_rows=0, continuation_loss=0.)

    monkeypatch.setattr(experiment, "_collect_one", collect)
    monkeypatch.setattr(experiment, "production_semantic_bundle", lambda: None)
    monkeypatch.setattr(experiment, "load_episode_training_rows", lambda _path: [object()])
    monkeypatch.setattr(experiment, "load_episode_validation_rows", lambda _path: [object()])
    monkeypatch.setattr(experiment, "EFFECTIVE_BATCH", 1)
    monkeypatch.setattr(experiment, "exposure_batches", lambda *_args, **_kwargs: [[(0, 0)]])
    monkeypatch.setattr(experiment, "train_generation_step", train_step)
    monkeypatch.setattr(experiment, "_run_paired_evaluation", lambda **kwargs: dict(
        score=dict(mean=.5), protocol=dict(games=4)))
    monkeypatch.setattr(experiment, "search_root_loss", lambda *_args: {
        "loss": torch.tensor(.75), "policy_loss": torch.tensor(.25),
        "value_loss": torch.tensor(.5), "continuation_loss": torch.tensor(0.),
        "continuation_rows": 0})

    result = run_next_generation(run_dir=tmp_path, parent_generation=1,
        train_games=1, validation_games=1)

    assert len(collection_models) == 2
    assert result["generation"] == 2
    assert result["parent_model_sha256"] == parent_sha
    assert result["checkpoint_restore"]["optimizer_steps"] == 2
    assert result["temporal_augmentation"] is False
    status = json.loads((tmp_path / "postgame_status.json").read_text())
    assert status["status"] == "complete" and status["generation"] == 2


def test_continuation_rejects_running_orchestrator_without_owner_pid(tmp_path):
    (tmp_path / "run_config.json").write_text(json.dumps(dict(
        schema="sspi-mac-pilot-run.v1", arm="A_5PCT_128", device="cpu")))
    (tmp_path / "postgame_status.json").write_text(json.dumps(dict(status="running")))

    with pytest.raises(RuntimeError, match="owner"):
        run_next_generation(run_dir=tmp_path, parent_generation=1)


def test_fresh_model_has_no_history_parameters_and_neutral_wdl_bias():
    model = initialize_fresh_model(20261101)
    same_seed = initialize_fresh_model(20261101)
    assert 7_000_000 <= model.parameter_count <= 10_500_000
    assert not any("event_encoder" in name or "previous_action_encoder" in name
        or "lstm" in name.lower() for name, _ in model.named_parameters())
    assert torch.count_nonzero(model.wdl_head[-1].weight) > 0
    assert torch.count_nonzero(model.wdl_head[-1].bias) == 0
    assert all(torch.equal(value, same_seed.state_dict()[name])
        for name, value in model.state_dict().items())


def test_no_search_evaluation_gate_is_deterministically_direct():
    selected, gate_seed = decision_gate(20261101, run_id="run", arm="A_5PCT_128",
        game_id="E-game-0001", owner=0, decision_index=12, probability=0.)
    repeated, repeated_seed = decision_gate(20261101, run_id="run", arm="A_5PCT_128",
        game_id="E-game-0001", owner=0, decision_index=12, probability=0.)
    assert selected is repeated is False
    assert gate_seed == repeated_seed


def test_exposure_schedule_uses_full_batches_unique_roots_and_respects_cap():
    roots = list(range(300))
    schedule = exposure_batches(roots, seed=20261101, exposure_limit=2, batch_size=128)
    assert len(schedule) == 4
    exposure_counts = [0] * len(roots)
    for batch in schedule:
        indices = [index for index, _exposure in batch]
        assert len(indices) == 128 and len(set(indices)) == 128
        for index in indices:
            exposure_counts[index] += 1
    assert max(exposure_counts) == 2


def test_paired_bootstrap_interval_is_seeded_and_reports_pair_mean():
    scores = [0., 0.5, 1., 0.5]
    first = paired_score_interval(scores, seed=44, replicates=200)
    second = paired_score_interval(scores, seed=44, replicates=200)
    assert first == second
    assert first["mean"] == pytest.approx(0.5)
    assert 0. <= first["low"] <= first["mean"] <= first["high"] <= 1.


def test_paired_evaluation_uses_same_native_random_stream_across_seat_swap():
    first = episode_seed_identity(20261101, run_id="run", arm="A_5PCT_128",
        game_id="E-paired-g0001-game-0001", random_stream_id="pair-0001")
    second = episode_seed_identity(20261101, run_id="run", arm="A_5PCT_128",
        game_id="E-paired-g0001-game-0002", random_stream_id="pair-0001")
    independent = episode_seed_identity(20261101, run_id="run", arm="A_5PCT_128",
        game_id="E-paired-g0001-game-0003", random_stream_id="pair-0002")

    assert first == second
    assert first["actual_match_seed"] != independent["actual_match_seed"]
    assert first["actual_rollout_seed"] != independent["actual_rollout_seed"]


def test_paired_score_validation_requires_complete_seed_matched_no_search_swaps():
    def result(pair, side, stream, match_seed, rollout_seed):
        return dict(pair_index=pair, side=side, game_id=f"g{pair}-{side}",
            trained_owner=side, score=float(side),
            random_stream_id=stream, actual_match_seed=match_seed,
            actual_rollout_seed=rollout_seed, search_roots=0, search_probability=0.)

    rows = [result(1, 0, "pair-1", 10, 20), result(1, 1, "pair-1", 10, 20),
        result(2, 0, "pair-2", 11, 21), result(2, 1, "pair-2", 11, 21)]
    assert paired_scores_from_results(rows, expected_pairs=2) == [0.5, 0.5]

    mismatched = deepcopy(rows)
    mismatched[1]["actual_match_seed"] = 99
    with pytest.raises(ValueError, match="share the same recorded random streams"):
        paired_scores_from_results(mismatched, expected_pairs=2)
    repeated_native_seeds = deepcopy(rows)
    repeated_native_seeds[2]["actual_match_seed"] = 10
    repeated_native_seeds[2]["actual_rollout_seed"] = 20
    repeated_native_seeds[3]["actual_match_seed"] = 10
    repeated_native_seeds[3]["actual_rollout_seed"] = 20
    with pytest.raises(ValueError, match="reused an actual native seed pair"):
        paired_scores_from_results(repeated_native_seeds, expected_pairs=2)
    with pytest.raises(ValueError, match="two complete games"):
        paired_scores_from_results(rows[:-1], expected_pairs=2)


def test_paired_evaluation_collects_both_seats_without_search_and_records_provenance(
        monkeypatch, tmp_path):
    import native_runner.training.sspi.experiment as experiment

    run_id, seed, arm = "paired-run", 20261101, "A_5PCT_128"
    run_config = dict(run_id=run_id, seed=seed, arm=arm, deck=list(HOG_DECK))

    def collect(**kwargs):
        game_id = kwargs["game_id"]
        game_number = int(game_id.rsplit("-", 1)[1])
        side = (game_number - 1) % 2
        stream_id = kwargs["random_stream_id"]
        trained_owner = side
        identity = dict(run_id=run_id, arm=arm, seed=seed, device="cpu", game_id=game_id,
            model_sha256_by_owner={str(side): "trained", str(1 - side): "theta0"},
            **episode_seed_identity(seed, run_id=run_id, arm=arm, game_id=game_id,
                random_stream_id=stream_id))
        metadata = dict(schema="sspi-mac-pilot-episode.v1", status="complete",
            episode_kind="evaluation", training_admissible=False,
            search_probability=0., search_roots=0, identity=identity,
            result=dict(winner=trained_owner))
        directory = kwargs["run_dir"] / "episodes" / game_id
        directory.mkdir(parents=True)
        (directory / "episode.json").write_text(json.dumps(metadata))
        return directory, metadata

    monkeypatch.setattr(experiment, "_collect_one", collect)
    record = experiment._run_paired_evaluation(run_config=run_config, run_dir=tmp_path,
        generation=1, trained_model=object(), trained_model_sha="trained",
        theta0_model=object(), theta0_sha="theta0", checkpoint_sha="checkpoint",
        actual_port=26789, belief_port=26790, transport="persistent", evaluation_pairs=2,
        output_name="paired-evaluation.json")

    assert record["protocol"]["games"] == 4
    assert record["score"]["mean"] == 1.
    assert {row["side"] for row in record["results"]} == {0, 1}
    assert len({row["random_stream_id"] for row in record["results"]}) == 2


def test_generation_evaluation_restores_checkpoint_and_validates_theta0(monkeypatch, tmp_path):
    import hashlib
    import native_runner.training.sspi.experiment as experiment
    from native_runner.training.fair_search.contracts import semantic_digest

    seed, run_id = 20261101, "evaluation-restore-run"
    theta0 = initialize_fresh_model(seed)
    theta0_sha = semantic_digest(theta0.state_dict())
    model = initialize_fresh_model(seed)
    optimizer = create_optimizer(model)
    parameter = next(model.parameters())
    parameter.square().sum().backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    identity = dict(run_id=run_id, generation=1, seed=seed,
        architecture_sha=model.architecture_sha)
    checkpoint_path = tmp_path / "checkpoints" / "generation-0001.pt"
    checkpoint = save_checkpoint(checkpoint_path, model, optimizer,
        identity=identity, optimizer_steps=1)
    (tmp_path / "generation-0001.json").write_text(json.dumps(dict(
        schema="sspi-mac-pilot-generation.v1", run_id=run_id, generation=1,
        theta0_sha256=theta0_sha, checkpoint=checkpoint)))
    initialization_path = tmp_path / "initialization.pt"
    initialization_path.write_bytes(b"original initialization identity")
    initialization_sha = hashlib.sha256(initialization_path.read_bytes()).hexdigest()
    (tmp_path / "run_config.json").write_text(json.dumps(dict(
        schema="sspi-mac-pilot-run.v1", run_id=run_id, arm="A_5PCT_128",
        device="cpu", seed=seed, transport="persistent", deck=list(HOG_DECK),
        initialization=dict(path=str(initialization_path), file_sha=initialization_sha,
            model_sha=theta0_sha))))
    monkeypatch.setattr(experiment, "production_semantic_bundle", lambda: None)

    def evaluate(**kwargs):
        assert kwargs["generation"] == 1
        assert kwargs["trained_model_sha"] == checkpoint["model_sha"]
        assert kwargs["checkpoint_sha"] == checkpoint["file_sha"]
        assert all(not parameter.requires_grad for parameter in kwargs["trained_model"].parameters())
        assert all(not parameter.requires_grad for parameter in kwargs["theta0_model"].parameters())
        return dict(score=dict(mean=.75), protocol=dict(games=2))

    monkeypatch.setattr(experiment, "_run_paired_evaluation", evaluate)
    result = evaluate_generation(run_dir=tmp_path, generation=1, evaluation_pairs=1)

    assert result["score"]["mean"] == .75


def test_generation_evaluation_rejects_theta0_identity_mismatch(monkeypatch, tmp_path):
    import hashlib
    import native_runner.training.sspi.experiment as experiment
    from native_runner.training.fair_search.contracts import semantic_digest

    seed, run_id = 20261101, "evaluation-wrong-theta0"
    model = initialize_fresh_model(seed)
    optimizer = create_optimizer(model)
    parameter = next(model.parameters())
    parameter.square().sum().backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    identity = dict(run_id=run_id, generation=1, seed=seed,
        architecture_sha=model.architecture_sha)
    checkpoint_path = tmp_path / "checkpoints" / "generation-0001.pt"
    checkpoint = save_checkpoint(checkpoint_path, model, optimizer,
        identity=identity, optimizer_steps=1)
    (tmp_path / "generation-0001.json").write_text(json.dumps(dict(
        schema="sspi-mac-pilot-generation.v1", run_id=run_id, generation=1,
        theta0_sha256="wrong-theta0", checkpoint=checkpoint)))
    initialization_path = tmp_path / "initialization.pt"
    initialization_path.write_bytes(b"original initialization identity")
    (tmp_path / "run_config.json").write_text(json.dumps(dict(
        schema="sspi-mac-pilot-run.v1", run_id=run_id, arm="A_5PCT_128",
        device="cpu", seed=seed, transport="persistent", deck=list(HOG_DECK),
        initialization=dict(path=str(initialization_path),
            file_sha=hashlib.sha256(initialization_path.read_bytes()).hexdigest(),
            model_sha=semantic_digest(initialize_fresh_model(seed).state_dict())))))
    monkeypatch.setattr(experiment, "production_semantic_bundle", lambda: None)
    monkeypatch.setattr(experiment, "_run_paired_evaluation", lambda **_kwargs: pytest.fail(
        "evaluation must not start when theta0 identity mismatches"))

    with pytest.raises(ValueError, match="theta0 differs"):
        evaluate_generation(run_dir=tmp_path, generation=1, evaluation_pairs=1)
