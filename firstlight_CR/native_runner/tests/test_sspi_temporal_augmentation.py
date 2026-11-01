"""Ordinary-policy continuation tests: no MCTS, emulator, or network required."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_final_runtime import TickEnvironment
from native_runner.tests.test_sspi_mac_training import TinyPolicyValue, native_player
from native_runner.training.fair_final.native import FinalDecisionRuntime
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import RolloutSnapshot
from native_runner.training.sspi import augmentation
from native_runner.training.sspi.augmentation import (
    SAMPLE_INTERVAL_TICKS, TEMPORAL_AUX_WEIGHT, TemporalAugmenter,
    fork_tensorizer, serialize_information,
)
from native_runner.training.sspi.experiment import exposure_batches
from native_runner.training.sspi.learning import (
    ContinuationRow, TrainingRow, WDLLabel, load_episode_training_rows,
    search_root_loss, train_generation_step, train_search_step,
)
from native_runner.training.sspi.model import SSPIPolicy
from native_runner.training.sspi.protocol import (
    ActionMode, DELAY_MS, HandState, SSPIAction, SSPIInformation, SSPILegality,
    to_full_action,
)


def information(tick=100, owner=0):
    hand = HandState.from_native(native_player())
    plays = torch.zeros((4, 576, 5), dtype=torch.bool)
    plays[:, 17, :] = True
    legality = SSPILegality(hand.ready_card_ids, plays)
    return SSPIInformation(owner, tick, {"visible_tick": tick}, hand, legality)


def tensorizer_stub():
    result = SimpleNamespace(**{name: None for name in RolloutSnapshot.tensorizer_fields})
    result.tracker = []
    for name in RolloutSnapshot.tensorizer_caches:
        setattr(result, name, {})
    return result


def ordinary_action(info, delay_index):
    def logits(prefix):
        if not prefix:
            return torch.tensor([-100., 0., -100.])
        if prefix == (ActionMode.PLAY,):
            return torch.tensor([0., -100., -100., -100.])
        if len(prefix) == 2:
            return torch.zeros(576)
        result = torch.full((5,), -100.)
        result[delay_index] = 0.
        return result
    return SSPIPolicy(logits, info.legality).sample(torch.Generator().manual_seed(51))[0]


def run_plain_policy(monkeypatch, delay_index=3, *, event_tick=None, rejected=False,
        terminal_tick=None, illegal_sample_tick=None):
    def capture(environment, observation, tensorizer, *, catalog):
        tensorizer.tracker.append(observation.tick)
        info = information(observation.tick, observation.owner)
        if observation.tick == illegal_sample_tick:
            info.legality.plays[:] = False
        return info
    monkeypatch.setattr(augmentation, "capture_pending_information", capture)
    info = information()
    action = ordinary_action(info, delay_index)
    environment = TickEnvironment(events=() if event_tick is None else (event_tick,),
        rejected=(0,) if rejected else (), terminal_tick=terminal_tick)
    runtime = FinalDecisionRuntime(environment, wait_ticks=20)
    runtime.commit_simultaneous({0: to_full_action(action), 1: FullAction()},
        legal=lambda owner, selected: True)
    root = dict(serialize_information(info), root_id="plain-policy:000000", decision_index=0,
        visits=[(action.key, 128)], simulations=128)
    source_tensorizer = tensorizer_stub()
    recorder = TemporalAugmenter()
    recorder.register(root, runtime.clock.pending[0], source_tensorizer)
    for _ in range(DELAY_MS[delay_index] // 50):
        start = len(runtime.clock.evidence)
        runtime.advance()
        recorder.observe(environment=environment, observations=environment.observe_dual(),
            clock=runtime.clock, evidence=runtime.clock.evidence[start:], catalog=None)
        if runtime.clock.ready or runtime.ended:
            break
    assert source_tensorizer.tracker == []
    return root, recorder, runtime, action


@pytest.mark.parametrize("delay_index", range(5))
def test_plain_policy_collects_only_intermediate_ticks_and_decreases_delay(monkeypatch, delay_index):
    root, recorder, runtime, action = run_plain_policy(monkeypatch, delay_index)
    samples = root["continuations"]
    assert len(samples) == delay_index
    assert [sample["tick"] for sample in samples] == list(range(104, runtime.clock.tick, 4))
    assert [sample["action"] for sample in samples] == [
        SSPIAction.play(action.card_id, action.cell, index).key
        for index in range(delay_index - 1, -1, -1)]
    assert environment_executions(runtime) == [(0, 100 + DELAY_MS[delay_index] // 50)]
    assert recorder.statistics()["retained"] == delay_index
    assert not recorder.pending


def environment_executions(runtime):
    return runtime.environment.executions


@pytest.mark.parametrize("kwargs", [dict(event_tick=108), dict(rejected=True), dict(terminal_tick=110)])
def test_preempted_rejected_and_terminal_expired_plans_discard_all_samples(monkeypatch, kwargs):
    root, recorder, runtime, _ = run_plain_policy(monkeypatch, **kwargs)
    assert root["continuations"] == []
    assert "continuation_execution" not in root
    assert recorder.statistics()["discarded"] > 0
    assert not recorder.pending
    # Preemption exactly at +400 ms must not produce a +400-ms old-plan frame.
    if kwargs.get("event_tick") == 108:
        assert recorder.captured == 1


def test_illegal_continuation_is_skipped_without_forcing_the_mask(monkeypatch):
    root, recorder, _, _ = run_plain_policy(monkeypatch, illegal_sample_tick=108)
    assert [sample["tick"] for sample in root["continuations"]] == [104, 112]
    assert recorder.retained == 2


def test_wait_produces_no_continuation_group():
    runtime = FinalDecisionRuntime(TickEnvironment(), wait_ticks=20)
    runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)
    recorder = TemporalAugmenter()
    root = {"owner": 0, "tick": 100}
    recorder.register(root, runtime.clock.pending[0], tensorizer_stub())
    assert not recorder.pending and "continuations" not in root


def test_tensorizer_fork_isolates_tracker_state_and_caches():
    source = tensorizer_stub()
    source._previous_action = {"action": [1]}
    cloned = fork_tensorizer(source)
    cloned.tracker.append(104)
    cloned._previous_action["action"].append(2)
    cloned._frame_tile_position_cache[10] = (1, 2)
    assert source.tracker == [] and source._previous_action == {"action": [1]}
    assert source._frame_tile_position_cache == {}


def training_row(*, count=3):
    source = information()
    action = SSPIAction.play(source.hand.ready_card_ids[0], 17, 3)
    extras = tuple(ContinuationRow(information(104 + 4 * index),
        SSPIAction.play(action.card_id, 17, 2 - index), WDLLabel.WIN) for index in range(count))
    return TrainingRow(source, source.legality, WDLLabel.WIN, "SEARCH",
        ((action, 128),), 128, continuations=extras)


def test_group_mean_keeps_three_correlated_samples_from_tripling_the_auxiliary_weight():
    model = TinyPolicyValue()
    full = search_root_loss(model, [training_row()])
    one = search_root_loss(model, [training_row(count=1)])
    baseline = search_root_loss(model, [training_row(count=0)])
    assert torch.allclose(full["loss"], one["loss"])
    assert full["continuation_rows"] == 3 and one["continuation_rows"] == 1
    assert full["loss"].item() == pytest.approx(baseline["loss"].item())
    assert TEMPORAL_AUX_WEIGHT == 0.5
    disabled = search_root_loss(model, [training_row()], temporal_aux_weight=0.)
    assert torch.equal(disabled["loss"], baseline["loss"])


def test_original_root_gets_half_and_continuations_share_the_other_half():
    model = TinyPolicyValue()
    row = training_row()
    selected = row.visits[0][0]
    # A mixed SEARCH distribution differs from the one-action auxiliary target.
    row = replace(row, visits=((selected, 32), (SSPIAction.wait(), 96)))
    source = search_root_loss(model, [replace(row, continuations=())])["loss"]
    policies, logits = model(row.continuations[0].frame, [row.continuations[0].frame.legality])
    extra = -policies[0].log_prob(row.continuations[0].action) + torch.nn.functional.cross_entropy(
        logits, torch.tensor([int(WDLLabel.WIN)]))
    actual = search_root_loss(model, [row])
    assert float(actual["loss"].detach()) == pytest.approx(float((0.5 * source + 0.5 * extra).detach()))


def test_auxiliary_samples_participate_in_a_real_backward_and_optimizer_update():
    model = TinyPolicyValue()
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    before = model.mode_logits.detach().clone()
    losses = train_search_step(model, optimizer, [training_row()])
    assert losses["continuation_rows"] == 3 and losses["continuation_loss"] > 0
    assert losses["gradient_norm"] > 0
    assert not torch.equal(before, model.mode_logits.detach())


def test_generation_microbatches_match_a_direct_128_group_update_and_share_exposure():
    rows = [training_row(count=index % 4) for index in range(128)]
    model = TinyPolicyValue()
    direct = deepcopy(model)
    first = torch.optim.AdamW(model.parameters(), lr=1e-3)
    second = torch.optim.AdamW(direct.parameters(), lr=1e-3)
    accumulated = train_generation_step(model, first, rows)
    expected = train_search_step(direct, second, rows)
    assert accumulated["continuation_rows"] == 192
    assert accumulated["loss"] == pytest.approx(float(expected["loss"].detach()), rel=1e-6)
    assert all(torch.allclose(value, direct.state_dict()[key], atol=1e-7)
        for key, value in model.state_dict().items())
    batches = exposure_batches(rows, seed=22)
    assert len(batches) == 2 and all(len(batch) == 128 for batch in batches)


def write_episode(directory, root, statistics):
    root["wdl"] = "WIN"
    for sample in root.get("continuations", ()):
        sample["wdl"] = "WIN"
    ledger = dict(rows=[dict(owner=0, tick=100, decision_index=0, mode="SEARCH",
        action=root["selected_action"], fair_sha256=root["fair_sha256"])],
        reservation_evidence=[root["continuation_execution"]])
    (directory / "actions.json").write_text(json.dumps(ledger))
    torch.save(dict(schema="sspi-mac-pilot-episode.v1", identity={}, roots=[root]), directory / "episode.pt")
    metadata = dict(schema="sspi-mac-pilot-episode.v1", status="complete", identity={},
        episode_kind="training", training_admissible=True, search_roots=1,
        tensor_sha256=hashlib.sha256((directory / "episode.pt").read_bytes()).hexdigest(),
        actions_sha256=hashlib.sha256((directory / "actions.json").read_bytes()).hexdigest(),
        result=dict(labels=["WIN", "LOSS"], terminal_tick=200), temporal_augmentation=statistics)
    (directory / "episode.json").write_text(json.dumps(metadata))


def test_collected_samples_roundtrip_into_the_training_loss(monkeypatch, tmp_path):
    root, recorder, _, _ = run_plain_policy(monkeypatch)
    write_episode(tmp_path, root, recorder.statistics())
    rows = load_episode_training_rows(tmp_path)
    assert len(rows) == 1 and len(rows[0].continuations) == 3
    assert [extra.frame.tick for extra in rows[0].continuations] == [104, 108, 112]
    assert all(extra.final_wdl == WDLLabel.WIN for extra in rows[0].continuations)
    assert search_root_loss(TinyPolicyValue(), rows)["continuation_rows"] == 3


@pytest.mark.parametrize("mutation", ["delay", "source", "wdl", "executed", "duplicate"])
def test_loader_rejects_misaligned_labels_even_with_recomputed_episode_checksum(monkeypatch, tmp_path, mutation):
    root, recorder, _, _ = run_plain_policy(monkeypatch)
    write_episode(tmp_path, root, recorder.statistics())
    payload = torch.load(tmp_path / "episode.pt", weights_only=True)
    changed = payload["roots"][0]
    if mutation == "delay":
        changed["continuations"][0]["action"] = changed["selected_action"]
    elif mutation == "source":
        changed["continuations"][0]["source_root_id"] = "another-root"
    elif mutation == "wdl":
        changed["continuations"][0]["wdl"] = "LOSS"
    elif mutation == "executed":
        changed["continuation_execution"]["reason"] = "preempted"
    else:
        changed["continuations"][1] = deepcopy(changed["continuations"][0])
    torch.save(payload, tmp_path / "episode.pt")
    metadata = json.loads((tmp_path / "episode.json").read_text())
    metadata["tensor_sha256"] = hashlib.sha256((tmp_path / "episode.pt").read_bytes()).hexdigest()
    (tmp_path / "episode.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="continuation"):
        load_episode_training_rows(tmp_path)


def test_loader_keeps_legacy_episodes_without_augmentation_readable(monkeypatch, tmp_path):
    root, recorder, _, _ = run_plain_policy(monkeypatch)
    write_episode(tmp_path, root, recorder.statistics())
    payload = torch.load(tmp_path / "episode.pt", weights_only=True)
    payload["roots"][0].pop("continuations")
    torch.save(payload, tmp_path / "episode.pt")
    metadata = json.loads((tmp_path / "episode.json").read_text())
    metadata.pop("temporal_augmentation")
    metadata.pop("actions_sha256")
    metadata["tensor_sha256"] = hashlib.sha256((tmp_path / "episode.pt").read_bytes()).hexdigest()
    (tmp_path / "episode.json").write_text(json.dumps(metadata))
    assert load_episode_training_rows(tmp_path)[0].continuations == ()


def test_real_fair_tensorization_and_full_model_backward_preserve_the_actor_tensorizer(monkeypatch):
    from native_runner.contracts import frozen_mapping
    from native_runner.tests.test_fair_final_history import opening_fixture
    from native_runner.training.fair_final.information import build_fair_tensorizer
    from native_runner.training.sspi.model import initialize_fresh_model

    observation, _ = opening_fixture()
    player = observation.players[0]
    metadata = dict(player.metadata)
    metadata["hand_runtime_by_slot"] = {str(index): {"form_code": 0}
        for index in range(4)}
    player = replace(player, metadata=frozen_mapping(metadata))
    mask = replace(observation.action_mask, reasons=frozen_mapping(
        {**dict(observation.action_mask.reasons), "effective_elixir": 5.}))
    observation = replace(observation, episode_id="temporal-real-model-unit",
        players=(player, observation.players[1]), action_mask=mask)
    actor_tensorizer = build_fair_tensorizer(observation)
    actor_tensorizer.start_episode(observation, initial_elixir={0: 5., 1: 5.})
    source = replace(information(), frame=actor_tensorizer.tensorize(observation))
    auxiliary_tensorizer = fork_tensorizer(actor_tensorizer)
    monkeypatch.setattr(SSPILegality, "from_native", classmethod(
        lambda cls, environment, observation, frame, owner, **kwargs: information().legality))
    environment = SimpleNamespace(raw_observation={"players": [dict(native_player(), owner=0)]})
    extras = []
    for index, tick in enumerate((104, 108, 112)):
        view = replace(observation, tick=tick)
        sample = augmentation.capture_pending_information(environment, view, auxiliary_tensorizer, catalog=None)
        extras.append(ContinuationRow(sample,
            SSPIAction.play(source.hand.ready_card_ids[0], 17, 2 - index), WDLLabel.WIN))
    assert actor_tensorizer._last_tensorized_tick == 100
    assert auxiliary_tensorizer._last_tensorized_tick == 112
    assert actor_tensorizer.tracker is not auxiliary_tensorizer.tracker
    # Extra sampling must not make the next actor observation go backwards.
    actor_tensorizer.tensorize(replace(observation, tick=108))

    row = TrainingRow(source, source.legality, WDLLabel.WIN, "SEARCH",
        ((SSPIAction.play(source.hand.ready_card_ids[0], 17, 3), 128),), 128,
        continuations=tuple(extras))
    torch.set_num_threads(1)
    model = initialize_fresh_model(20261101)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-4)
    before = model.wdl_head[-1].weight.detach().clone()
    result = train_search_step(model, optimizer, [row])
    assert result["continuation_rows"] == 3 and result["gradient_norm"] > 0
    assert not torch.equal(before, model.wdl_head[-1].weight.detach())
