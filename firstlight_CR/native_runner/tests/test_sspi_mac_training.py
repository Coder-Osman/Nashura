from copy import deepcopy
import json
from types import SimpleNamespace

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
    HogRefillClock,
    SSPILegality,
    SSPIAction,
    SSPIInformation,
    from_full_action,
    to_full_action,
)
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_final.policy import FullAction
from native_runner.contracts import ObservationTier
from native_runner.cr_native_env import NativeClashEnv, ResidentNativeClashEnv, RunnerError
from native_runner.training.sspi.collector import (
    ARM_CONFIG,
    EventBatchedSSPINative,
    OwnerBeliefProviders,
    SearchNative,
    _native_execution_action,
    _observe_histories,
    decision_gate,
    episode_seed_identity,
    preflight_native_endpoints,
    resolve_belief_ports,
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
import native_runner.training.sspi.experiment as experiment_module


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
    hand = HandState.from_native(native_player((0, 2)), refill_remaining_ticks=(12, 32))
    features = _hand_features(hand, device="cpu")
    assert features.shape == (HAND_FEATURE_DIM,)
    assert features[0] == 0. and features[15] == 1.
    assert features[13] == 1. and features[14] == pytest.approx(12 / 1200)
    assert features[43] == 1. and features[44] == pytest.approx(32 / 1200)
    refilled = HandState.from_native(refill_one(native_player((0, 2))))
    assert _hand_features(refilled, device="cpu").shape == features.shape


def test_hog_refill_clock_preserves_deadline_across_batched_ticks_and_restore():
    clock = HogRefillClock()
    before = {owner: native_player() for owner in (0, 1)}
    first = deepcopy(before)
    first[0]["hand"][0] = {**first[0]["deck"][4], "handIndex": 0}
    clock.advance(before, first, 401, 402, {0: (402,), 1: ()})
    assert clock.remaining(0, 405, 2) == (17, 37)
    saved = clock.capture()
    second = deepcopy(first)
    second[0]["hand"].pop(0)
    clock.advance(first, second, 402, 405, {0: (405,), 1: ()})
    assert clock.remaining(0, 405, 1) == (17,)
    refilled = deepcopy(second)
    refilled[0]["hand"].append({**refilled[0]["deck"][5], "handIndex": 0})
    clock.advance(second, refilled, 405, 422, {0: (), 1: ()})
    assert clock.remaining(0, 422, 1) == (20,)
    clock.restore(saved)
    assert clock.remaining(0, 405, 2) == (17, 37)
    clock.advance(first, first, 405, 450, {0: (), 1: ()})
    late = deepcopy(first)
    late[0]["hand"][0] = {**late[0]["deck"][5], "handIndex": 0}
    clock.advance(first, late, 450, 451, {0: (451,), 1: ()})
    assert clock.remaining(0, 455, 1) == (16,)
    assert clock.next_boundary({0: native_player((0,)), 1: native_player()}, 455) == 471


def test_early_pending_play_is_ignored_without_changing_policy_target():
    plays = torch.zeros((1, 576, 5), dtype=torch.bool)
    plays[0, 17, :] = True
    legality = SSPILegality((HOG_DECK[4],), plays,
        pending_arrival_ticks={HOG_DECK[4]: 12})
    early = SSPIAction.play(HOG_DECK[4], 17, 0)
    later = SSPIAction.play(HOG_DECK[4], 17, 4)
    assert legality.legal(early)
    assert legality.execution_action(to_full_action(early)) == FullAction()
    assert legality.execution_action(to_full_action(later)) == to_full_action(later)


def test_pending_card_candidate_exposes_arrival_time_and_early_bin(monkeypatch):
    from native_runner.training.sspi import protocol

    player = native_player((0,))
    for card in (*player["hand"], *player["deck"]):
        card["cost"] = 2
    hand = HandState.from_native(player, refill_remaining_ticks=(12,))
    count = len(HOG_DECK)
    cells = torch.zeros((count, 576), dtype=torch.bool)
    for row in range(1, 4):
        cells[row, 17] = True
    legacy = SimpleNamespace(decision_ready=True, row_card_id=torch.tensor(HOG_DECK),
        cell_masks=cells, delay_masks=torch.ones((count, 31), dtype=torch.bool))
    frame = SimpleNamespace(own_cards=SimpleNamespace(
        card_vocab_id=torch.zeros((1, count), dtype=torch.long),
        mask=torch.ones((1, count), dtype=torch.bool)))
    observation = SimpleNamespace(owner=0, tick=410, tier=ObservationTier.FAIR)
    raw = {"tick": 410, "players": [{**player, "owner": 0, "elixirRaw": 100000}]}
    environment = SimpleNamespace(current_tick=410, raw_observation=raw,
        mode_timeline=SimpleNamespace(timeline=object()),
        _extract_towers=lambda raw: (), _rich_context=lambda raw: None,
        _occupied_building_footprints=lambda raw, rich: ((), ()),
        _destroyed_enemy_lanes=lambda owner, towers: (),
        _active_tower_footprints=lambda towers: (),
        _cached_placement_entry=lambda **kwargs: {"row_major": tuple(
            tuple(cell == 17 for cell in range(18)) if row == 0 else (False,) * 18
            for row in range(32))})
    monkeypatch.setattr(protocol, "validate_fair_view", lambda *args: None)
    monkeypatch.setattr(protocol, "legal_event_actions", lambda *args, **kwargs: legacy)
    monkeypatch.setattr(protocol, "native_payment_mask", lambda **kwargs: torch.ones(31, dtype=torch.bool))
    monkeypatch.setattr(protocol, "gameplay_end_tick", lambda environment: 7200)
    monkeypatch.setattr(protocol, "constrain_spell_placement", lambda rows, *args: rows)
    legality = SSPILegality.from_native(environment, observation, frame, 0,
        catalog=object(), hand_state=hand)
    assert legality.pending_arrival_ticks == {HOG_DECK[0]: 12}
    assert legality.legal(SSPIAction.play(HOG_DECK[0], 17, 0))


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


def test_event_batch_collector_reuses_validated_json_capture_and_invalidates_before_control(monkeypatch):
    calls = []
    capture = {"tick": 8, "ordinary": {"tick": 8}, "rich": {"tick": 8}}
    profile = dict(connect_ns=1, batches=1, environments=1, request_bytes=10,
        response_bytes=20, decoded_response_bytes=30, send_ns=2,
        request_ack_ns=3, last_request_ack_ns=3, receive_ns=4, decode_ns=5, total_ns=6)

    class Channel:
        def __init__(self, host, port, **options):
            calls.append(("connect", host, port, options))

        def step_partitioned(self, actions, *, ticks):
            calls.append(("step", actions, ticks))
            return {0: type("Outcome", (), {"capture": capture, "stop_flags": 2,
                "stopped_on_deployment": True, "stopped_on_terminal": False,
                "reached_max_ticks": False})()}

        def profile(self):
            return profile

        def close(self):
            calls.append("close")

    def request(native, command, **options):
        calls.append(("control", command))
        if command == "multi-status":
            return {"occupied": 1, "slots": [{"envId": 0}]}
        return {"ok": True}

    monkeypatch.setattr("native_runner.training.sspi.collector.ResidentBatchChannelV1", Channel)
    monkeypatch.setattr(NativeClashEnv, "_request", request)
    native = EventBatchedSSPINative("127.0.0.1", 26789)
    native.start_atomic_steps(initial_tick=7)
    assert native.step(5) is capture["ordinary"]
    assert native.observe_atomic() is capture
    assert native.observe() is capture["ordinary"]
    assert calls[0][3]["training_capture"] is False
    assert calls[0][3]["compressed_json"] is False
    assert calls[0][3]["validate_json_capture"] is True
    assert calls[0][3]["step_until_event"] is True
    assert calls[1] == ("step", {0: ()}, 5)
    assert native.last_advance_boundary.start_tick == 7
    assert native.last_advance_boundary.end_tick == 8
    assert native.last_advance_boundary.stop_reason == "deployment"

    native._request('inject {"owner":0}')
    assert calls[-2:] == ["close", ("control", 'env 0 inject {"owner":0}')]
    assert native._capture is None
    assert native.capture_profile["response_bytes"] == 20
    result = native.finish_capture_profile()
    assert result["decoded_response_bytes"] == 30
    assert result["actual_native_ticks"] == 1
    assert result["requested_native_ticks"] == 5
    assert result["stopped_on_deployment"] == 1
    native.close_transport()


def test_event_batch_snapshot_controls_require_one_resident_slot(monkeypatch):
    native = EventBatchedSSPINative("127.0.0.1", 26789)
    monkeypatch.setattr(ResidentNativeClashEnv, "resident_status",
        lambda _self: {"occupied": 2, "slots": [{"envId": 0}, {"envId": 1}]})
    with pytest.raises(RunnerError, match="exactly one resident slot"):
        native._request("snapshot-create")


def test_event_batch_transport_routes_its_configured_resident_slot(monkeypatch):
    calls = []
    capture = {"tick": 9, "ordinary": {"tick": 9}, "rich": {"tick": 9}}

    class Channel:
        def __init__(self, host, port, **options):
            calls.append(("connect", options))

        def step_partitioned(self, actions, *, ticks):
            calls.append(("step", actions, ticks))
            return {1: type("Outcome", (), {"capture": capture, "stop_flags": 0,
                "stopped_on_deployment": False, "stopped_on_terminal": False,
                "reached_max_ticks": True})()}

        def profile(self):
            return {name: 0 for name in EventBatchedSSPINative._CHANNEL_PROFILE_FIELDS}

        def close(self):
            calls.append("close")

    monkeypatch.setattr("native_runner.training.sspi.collector.ResidentBatchChannelV1", Channel)
    native = EventBatchedSSPINative("127.0.0.1", 26789, env_id=1)
    native.start_atomic_steps(initial_tick=8)
    assert native.step(4) is capture["ordinary"]
    assert calls[-1] == ("step", {1: ()}, 4)
    assert native.last_advance_boundary.stop_reason == "tick_budget"
    native.close_transport()


def test_event_batch_snapshot_routes_single_slot_digest_through_global_control(monkeypatch):
    calls = []
    native = EventBatchedSSPINative("127.0.0.1", 26789)
    monkeypatch.setattr(ResidentNativeClashEnv, "resident_status",
        lambda _self: {"occupied": 1, "slots": [{"envId": 0}]})
    monkeypatch.setattr(NativeClashEnv, "_request",
        lambda _self, command, **_options: calls.append(command) or {"ok": True})

    assert native._request("snapshot-create") == {"ok": True}
    assert calls == ["snapshot-create"]


def test_owner_belief_providers_keep_independent_history_cursors_and_close_all():
    calls = []

    class Provider:
        def __init__(self, owner, port, history):
            self.owner = owner
            self.port = port
            self.histories = [history]
            self.closed = 0

        def update_history(self, history):
            self.histories.append(history)

        def close(self):
            self.closed += 1

    def factory(owner, port, history):
        provider = Provider(owner, port, history)
        calls.append(provider)
        return provider

    providers = OwnerBeliefProviders(resolve_belief_ports(26789, 26790), factory)
    first_zero = providers.for_search(0, "zero-opening")
    first_one = providers.for_search(1, "one-opening")
    assert providers.for_search(0, "zero-later") is first_zero
    assert providers.for_search(1, "one-later") is first_one
    assert [(item.owner, item.port, item.histories) for item in calls] == [
        (0, 26790, ["zero-opening", "zero-later"]),
        (1, 26791, ["one-opening", "one-later"]),
    ]

    providers.close()
    providers.close()
    assert [item.closed for item in calls] == [1, 1]


def test_belief_port_identity_requires_three_distinct_native_endpoints():
    assert resolve_belief_ports(26789, 26790) == {0: 26790, 1: 26791}
    assert resolve_belief_ports(26789, 26790, {0: 26791, 1: 26792}) == {
        0: 26791, 1: 26792}
    for actual, default, mapping in (
            (26789, 26790, {0: 26790, 1: 26790}),
            (26789, 26790, {0: 26789, 1: 26791}),
            (26789, 65535, None),
            (True, 26790, None)):
        with pytest.raises(ValueError, match="port"):
            resolve_belief_ports(actual, default, mapping)


def test_native_preflight_checks_readable_observations_and_matching_artifacts(monkeypatch):
    calls = []

    class FakeNative:
        def __init__(self, host, port, *, transport, timeout):
            self.port = port
            calls.append(("open", host, port, transport, timeout))

        def status(self):
            calls.append(("status", self.port))
            return {"ready": True}

        def observe(self):
            calls.append(("observe", self.port))
            return {"ok": True}

        def attest(self, ruleset_manifest=None):
            calls.append(("attest", self.port))
            return type("Attestation", (), {
                "libg_sha256": "a" * 64, "probe_sha256": "b" * 64})()

        def close_transport(self):
            calls.append(("close", self.port))

    monkeypatch.setattr("native_runner.training.sspi.collector.SearchNative", FakeNative)
    result = preflight_native_endpoints("127.0.0.1", 26789,
        {0: 26790, 1: 26792}, transport="persistent")

    assert set(result) == {"actual", "belief_owner_0", "belief_owner_1"}
    assert all(item == {"libg_sha256": "a" * 64, "probe_sha256": "b" * 64}
        for item in result.values())
    assert [call[0] for call in calls].count("observe") == 3
    assert [call[0] for call in calls].count("close") == 3

    calls.clear()
    evaluation = preflight_native_endpoints("127.0.0.1", 26789,
        {0: 26790, 1: 26792}, transport="persistent", include_belief=False)
    assert set(evaluation) == {"actual"}
    assert [call[0] for call in calls].count("observe") == 1
    assert [call[0] for call in calls].count("close") == 1


@pytest.mark.parametrize("frozen_owners, expected_calls", [
    ((0, 1), ()), ((0,), ("observe:1",)), ((), ("observe_dual",)),
])
def test_history_observation_reuses_frozen_views(monkeypatch, frozen_owners, expected_calls):
    calls = []
    views = {owner: SimpleNamespace(owner=owner, tick=118) for owner in (0, 1)}
    boundary = object()

    class Environment:
        native = SimpleNamespace(last_advance_boundary=boundary)

        def observe_dual(self, *, policy_features_only):
            assert policy_features_only
            calls.append("observe_dual")
            return dict(views)

        def observe(self, *, owner, policy_features_only):
            assert policy_features_only
            calls.append(f"observe:{owner}")
            return views[owner]

    class History:
        def __init__(self):
            self.received = []

        def observe_tick(self, observation, *, interval):
            self.received.append((observation, interval))

    histories = {owner: History() for owner in (0, 1)}
    runtime = SimpleNamespace(environment=Environment(),
        frozen={owner: views[owner] for owner in frozen_owners})
    monkeypatch.setattr("native_runner.training.sspi.collector.validate_fair_view",
        lambda observation, owner: None if observation.owner == owner else pytest.fail("wrong owner"))

    observed = _observe_histories(SimpleNamespace(runtime=runtime), histories)

    assert tuple(calls) == expected_calls
    assert all(observed[owner] is views[owner] for owner in (0, 1))
    assert all(histories[owner].received == [(views[owner], boundary)] for owner in (0, 1))


def test_native_preflight_rejects_truncated_observation_and_closes_transport(monkeypatch):
    closed = []

    class FakeNative:
        def __init__(self, *_args, **_kwargs):
            pass

        def status(self):
            return {"ready": True}

        def observe(self):
            raise ValueError("invalid native runner response to 'observe'")

        def close_transport(self):
            closed.append(True)

    monkeypatch.setattr("native_runner.training.sspi.collector.SearchNative", FakeNative)
    with pytest.raises(ValueError, match="invalid native runner response"):
        preflight_native_endpoints("127.0.0.1", 26789,
            {0: 26790, 1: 26792}, transport="persistent")
    assert closed == [True]


@pytest.mark.parametrize("transport", ["event-json", "event-json-delta"])
def test_event_json_preflight_selects_resident_slot_without_rebinding_frozen_slot(monkeypatch, transport):
    calls = []

    class Probe:
        def __init__(self, host, port, *, transport, timeout):
            self.port = port
            calls.append(("probe", port, transport))

        def _request(self, command):
            assert command == "multi-status"
            return {"mode": "resident-headless", "occupied": 2,
                "slots": [{"envId": 0}, {"envId": 1}]}

        def status(self):
            return {"ready": True}

        def observe(self):
            return {"ok": True}

        def attest(self, _ruleset=None):
            return type("Attestation", (), {
                "libg_sha256": "a" * 64, "probe_sha256": "b" * 64})()

        def close_transport(self):
            calls.append(("probe-close", self.port))

    class Resident:
        def __init__(self, env_id, host, port, *, timeout):
            calls.append(("resident", env_id, port))
            self.port = port

        def status(self):
            return {"ready": True}

        def observe(self):
            return {"ok": True}

        def attest(self, _ruleset=None):
            return type("Attestation", (), {
                "libg_sha256": "a" * 64, "probe_sha256": "b" * 64})()

        def close_transport(self):
            calls.append(("resident-close", self.port))

    monkeypatch.setattr("native_runner.training.sspi.collector.SearchNative", Probe)
    monkeypatch.setattr("native_runner.training.sspi.collector.ResidentNativeClashEnv", Resident)

    result = preflight_native_endpoints("127.0.0.1", 26789,
        {0: 26790, 1: 26792}, transport=transport, actual_env_id=1)

    assert calls[2] == ("resident", 1, 26789)
    assert set(result) == {"actual", "belief_owner_0", "belief_owner_1"}


@pytest.mark.parametrize("transport", ["persistent", "event-json-delta"])
def test_experiment_routes_run_config_owner_ports_into_episode(monkeypatch, tmp_path, transport):
    captured = {}

    def collect_episode(**kwargs):
        captured.update(kwargs)
        return {"status": "complete"}

    monkeypatch.setattr(experiment_module, "collect_episode", collect_episode)
    run_config = dict(run_id="ports-test", arm="A_5PCT_128", seed=20261102,
        actual_env_id=1, ports={"belief_by_owner": {"0": 26791, "1": 26792}})
    output, episode = experiment_module._collect_one(run_config=run_config,
        run_dir=tmp_path, game_id="A-game-0001", episode_kind="training",
        model=object(), device="cpu", actual_port=26789, belief_port=26790,
        transport=transport)

    assert output == tmp_path / "episodes" / "A-game-0001"
    assert episode["status"] == "complete"
    assert captured["belief_ports"] == {0: 26791, 1: 26792}
    assert captured["actual_env_id"] == 1
    assert captured["transport"] == transport


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
    identity = {"run": "unit-test", "seed": 20261101, "action_schema": "sspi-tagged-action.v2"}
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
