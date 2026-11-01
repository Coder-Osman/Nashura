from copy import deepcopy

import pytest
import torch
from torch import nn

from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.sspi.learning import (
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


def test_explicit_sspi_search_budget_runs_all_1024_effective_simulations():
    from native_runner.tests.test_fair_final_search import search_fixture

    limits = SearchLimits(root_noise=.25, root_noise_mode="training")
    search, info, arguments = search_fixture(limits=limits)
    arguments["identity"]["search_protocol_sha"] = budget_search_sha(limits, 1024)
    outcome = search.run(info, simulation_budget=1024, **arguments)
    assert outcome.result["valid_simulations"] == 1024
    assert sum(entry["visits"] for entry in outcome.result["candidates"]) == 1024
    assert sum(count for _, count in outcome.policy_target) == 1024
    assert outcome.result["search_protocol_sha"] == budget_search_sha(limits, 1024)
    assert search.belief.world.closed


def test_search_budget_identities_are_separate_and_fixed():
    limits = SearchLimits(root_noise=.25, root_noise_mode="training")
    assert budget_search_sha(limits, 1024) != budget_search_sha(limits, 128)
    with pytest.raises(ValueError, match="budget"):
        budget_search_sha(limits, 256)


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
        ((SSPIAction.play(HOG_DECK[0], 17, 2), 1024),), 1024)
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
