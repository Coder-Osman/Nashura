import hashlib
import json
from pathlib import Path
import shutil

import pytest
import torch
from torch import nn

from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.sspi.augmentation import serialize_information
from native_runner.training.sspi.collector import (VALUE_ARM, VALUE_EPISODE_SCHEMA,
    value_sample_gate)
from native_runner.training.sspi.learning import (WLLabel, load_checkpoint,
    save_checkpoint)
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.protocol import HandState, SSPILegality, SSPIInformation
from native_runner.training.sspi.value_learning import (ValueRow,
    create_value_optimizer, load_value_episode, policy_head_sha,
    policy_function_sha, prediction_metrics, train_value_curve_batch,
    train_value_generation)
from native_runner.training.sspi.value_curve import (_paired_game_bootstrap,
    _constant_predictions, _evaluate, _seed_for, _check_head_scope)


class TinyValueModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.shared = nn.Linear(2, 4)
        self.policy_head = nn.Linear(4, 3)
        self.value_head = nn.Linear(4, 2)

    def value_only(self, information):
        return self.value_head(self.shared(information).tanh())


def test_value_gate_is_deterministic_and_independent_by_owner_and_decision():
    calls = [value_sample_gate(20261103, run_id="value-test", game_id="game-1",
        owner=owner, decision_index=index, probability=.5)
        for owner in (0, 1) for index in range(20)]
    assert calls == [value_sample_gate(20261103, run_id="value-test", game_id="game-1",
        owner=owner, decision_index=index, probability=.5)
        for owner in (0, 1) for index in range(20)]
    assert len({seed for _, seed in calls}) == 40
    assert 0 < sum(selected for selected, _ in calls) < 40
    assert value_sample_gate(20261103, run_id="value-test", game_id="game-1",
        owner=0, decision_index=0, probability=0.)[0] is False


def test_value_training_changes_shared_and_value_parameters_but_not_policy_head():
    torch.manual_seed(11)
    model = TinyValueModel()
    optimizer = create_value_optimizer(model)
    head_before = policy_head_sha(model)
    shared_before = model.shared.weight.detach().clone()
    rows = [ValueRow(f"train-{i // 16}", torch.tensor([[1., .25]]), WLLabel.WIN)
        for i in range(129)]
    validation = [ValueRow(f"validation-{i // 8}", torch.tensor([[1., .25]]),
        WLLabel.WIN) for i in range(32)]
    result = train_value_generation(model, optimizer, rows, validation, seed=7)
    assert result["attempted_steps"] == 2
    assert result["validation_after"]["wl_ce"] < result["validation_before"]["wl_ce"]
    assert result["policy_head_sha"] == policy_head_sha(model) == head_before
    assert all(parameter.grad is None for parameter in model.policy_head.parameters())
    assert not torch.equal(shared_before, model.shared.weight)
    assert len(optimizer.state) > 0


def test_value_metrics_reject_empty_and_report_calibrated_predictions():
    with pytest.raises(ValueError, match="held-out"):
        prediction_metrics([])
    metrics = prediction_metrics([("game-1", 0, (.8, .2)),
        ("game-2", 1, (.2, .8))])
    assert metrics["games"] == 2 and metrics["rows"] == 2
    assert metrics["wl_ce"] == pytest.approx(-torch.tensor(.8).log().item())
    assert metrics["brier"] == pytest.approx(.08)


def _value_episode(tmp_path):
    deck = [{"deckSlot": index, "cardId": card} for index, card in enumerate(HOG_DECK)]
    player = dict(deck=deck, hand=[{**deck[index], "handIndex": index} for index in range(4)],
        cycle=[{**deck[index], "cycleIndex": index - 4} for index in range(4, 8)],
        nextCard={**deck[4], "cycleIndex": 0})
    information = SSPIInformation(0, 100, {"fair": "frame"},
        HandState.from_native(player),
        SSPILegality((), torch.empty((0, 576, 5), dtype=torch.bool)))
    run_id, game_id, model_sha = "value-test", "train-g0001-0001", "model-sha"
    index = next(index for index in range(100)
        if value_sample_gate(20261103, run_id=run_id, game_id=game_id, owner=0,
            decision_index=index, probability=.5)[0])
    _, gate_seed = value_sample_gate(20261103, run_id=run_id, game_id=game_id,
        owner=0, decision_index=index, probability=.5)
    row = dict(**serialize_information(information), decision_index=index,
        value_gate_seed=gate_seed, action="wait", executed_action="wait", wl="WIN")
    identity = dict(run_id=run_id, game_id=game_id, arm=VALUE_ARM, seed=20261103,
        random_stream_id=game_id, collection_protocol="value-only.v2",
        model_sha256_by_owner={"0": model_sha, "1": model_sha},
        value_sample_probability=.5)
    tensor_path = tmp_path / "episode.pt"
    torch.save(dict(schema=VALUE_EPISODE_SCHEMA, identity=identity,
        roots=[], value_rows=[row]), tensor_path)
    action = dict(owner=0, decision_index=index, tick=100,
        fair_sha256=information.sha256, mode="DIRECT", value_selected=True,
        value_gate_seed=gate_seed, action="wait", executed_action="wait")
    action_path = tmp_path / "actions.json"
    action_path.write_text(json.dumps(dict(rows=[action])))
    metadata = dict(schema=VALUE_EPISODE_SCHEMA, status="complete", identity=identity,
        episode_kind="training", training_admissible=True, search_roots=0,
        search_probability=0., value_sample_probability=.5, value_samples=1,
        decisions=dict(decisions={"0": 1, "1": 0}, value_samples={"0": 1, "1": 0}),
        result=dict(labels=["WIN", "LOSS"], winner=0),
        supervision=dict(value_order=["WIN", "LOSS"], eligible=True, excluded_reason=None),
        tensor_sha256=hashlib.sha256(tensor_path.read_bytes()).hexdigest(),
        actions_sha256=hashlib.sha256(action_path.read_bytes()).hexdigest())
    (tmp_path / "episode.json").write_text(json.dumps(metadata))
    return run_id, model_sha, metadata


def test_value_episode_requires_complete_native_and_matching_decision_evidence(tmp_path):
    run_id, model_sha, metadata = _value_episode(tmp_path)
    rows, _ = load_value_episode(tmp_path, expected_kind="training",
        run_id=run_id, model_sha=model_sha)
    assert len(rows) == 1 and rows[0].final_wl == WLLabel.WIN
    metadata["status"] = "quarantined"
    (tmp_path / "episode.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="natural terminal"):
        load_value_episode(tmp_path, expected_kind="training",
            run_id=run_id, model_sha=model_sha)


def test_value_checkpoint_restores_filtered_adamw_state_and_frozen_head(tmp_path):
    model = initialize_fresh_model(20261103)
    optimizer = create_value_optimizer(model, head_only=True)
    head_sha = policy_head_sha(model)
    function_sha = policy_function_sha(model)
    optimizer.zero_grad(set_to_none=True)
    model.value_head[-1].bias[0].backward()
    optimizer.step()
    optimizer.zero_grad(set_to_none=True)
    identity = dict(protocol="sspi-value-only-run.v2", run_id="value-test",
        policy_head_sha=head_sha)
    checkpoint = save_checkpoint(tmp_path / "checkpoint.pt", model, optimizer,
        identity=identity, optimizer_steps=1)
    restored = initialize_fresh_model(20261103)
    restored_optimizer = create_value_optimizer(restored, head_only=True)
    result = load_checkpoint(tmp_path / "checkpoint.pt", restored, restored_optimizer,
        expected_identity=identity)
    assert result["file_sha"] == checkpoint["file_sha"]
    assert semantic_digest(restored.state_dict()) == checkpoint["model_sha"]
    assert policy_head_sha(restored) == head_sha
    assert policy_function_sha(restored) == function_sha
    assert all(name.startswith("value_head.")
        for name, parameter in restored.named_parameters() if parameter.requires_grad)
    assert len(restored_optimizer.state) == len(optimizer.state)


def test_checkpoint_load_skips_rng_state_for_unavailable_accelerators(tmp_path, monkeypatch):
    model = initialize_fresh_model(20261103)
    optimizer = create_value_optimizer(model, head_only=True)
    identity = dict(protocol="sspi-value-curve-portable-rng-test", run_id="value-test")
    path = tmp_path / "portable.pt"
    save_checkpoint(path, model, optimizer, identity=identity, optimizer_steps=0)
    envelope = torch.load(path, weights_only=True, map_location="cpu")
    payload = envelope["payload"]
    payload["mps_rng"] = torch.get_rng_state().clone()
    payload["cuda_rng"] = [torch.get_rng_state().clone()]
    envelope["payload_sha"] = semantic_digest(payload)
    torch.save(envelope, path)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    restored = initialize_fresh_model(20261103)
    restored_optimizer = create_value_optimizer(restored, head_only=True)
    result = load_checkpoint(path, restored, restored_optimizer,
        expected_identity=identity)

    assert result["model_sha"] == semantic_digest(restored.state_dict())
    assert not restored_optimizer.state


def _curve_rows(prefix, count):
    rows, features = [], []
    for index in range(count):
        game_number, decision = divmod(index, 32)
        owner = decision % 2
        decision_index = decision // 2
        game_id = f"{prefix}-{game_number:04d}"
        label = WLLabel((index // 3) % 2)
        rows.append(ValueRow(game_id, torch.tensor([[float(index % 7), 1.]]), label,
            owner=owner, decision_index=decision_index,
            fair_sha256=hashlib.sha256(f"{game_id}:{owner}:{decision_index}".encode()).hexdigest()))
        features.append([float(index % 5), float(index % 3), float(owner), 1.])
    return rows, torch.tensor(features, dtype=torch.float32)


def test_value_curve_exposes_each_state_once_and_keeps_policy_function_frozen():
    torch.manual_seed(23)
    model = TinyValueModel()
    optimizer = create_value_optimizer(model, head_only=True)
    rows, features = _curve_rows("train", 129)
    shared_before = model.shared.weight.detach().clone()
    policy_before = policy_head_sha(model)
    function_before = policy_function_sha(model)
    value_before = model.value_head.weight.detach().clone()

    first = train_value_curve_batch(model, optimizer, rows, seed=91,
        batch_size=128, microbatch_size=4, features=features)

    assert first["rows"] == 129 and first["games"] == 5
    assert first["optimizer_steps"] == 2
    assert not torch.equal(value_before, model.value_head.weight)
    assert torch.equal(shared_before, model.shared.weight)
    assert policy_head_sha(model) == policy_before
    assert policy_function_sha(model) == function_before
    assert all(parameter.grad is None for name, parameter in model.named_parameters()
        if not name.startswith("value_head."))
    first_step = int(optimizer.state[model.value_head.weight]["step"])

    next_rows, next_features = _curve_rows("next", 8)
    second = train_value_curve_batch(model, optimizer, next_rows, seed=92,
        batch_size=128, microbatch_size=4, features=next_features)
    assert second["optimizer_steps"] == 1
    assert int(optimizer.state[model.value_head.weight]["step"]) == first_step + 1
    with pytest.raises(ValueError, match="repeats"):
        train_value_curve_batch(model, optimizer, [rows[0], rows[0]], seed=93,
            features=torch.zeros((2, 4)))


def test_value_curve_uses_disjoint_seed_domains_and_familywise_game_bootstrap():
    train = {_seed_for("training", i) for i in range(1, 1025)}
    validation = {_seed_for("validation", i) for i in range(1, 33)}
    holdout = {_seed_for("holdout", i) for i in range(1, 33)}
    assert not (train & validation or train & holdout or validation & holdout)
    candidate, baseline = [], []
    for game in range(16):
        for owner in (0, 1):
            label = owner
            candidate.append((f"game-{game}", label,
                (.99, .01) if label == 0 else (.01, .99)))
            baseline.append((f"game-{game}", label, (.5, .5)))
    result = _paired_game_bootstrap(candidate, baseline, seed=17,
        alpha=.025, replicates=2000)
    assert result["games"] == 16
    assert result["upper"] < 0.
    assert result["one_sided_alpha"] == .025


def test_value_curve_frequency_baseline_uses_exact_training_row_frequencies():
    rows = [ValueRow("validation-1", None, WLLabel.WIN),
        ValueRow("validation-2", None, WLLabel.LOSS)]
    predictions = _constant_predictions(rows, [9, 1])
    assert predictions[0][2] == pytest.approx((.9, .1))
    assert predictions[1][2] == predictions[0][2]
    with pytest.raises(ValueError, match="requires decisive"):
        _constant_predictions(rows, [0, 0])


def test_value_curve_skips_empty_batches_and_marks_empty_validation_unevaluable():
    model = initialize_fresh_model(20261103)
    optimizer = create_value_optimizer(model, head_only=True)
    skipped = train_value_curve_batch(model, optimizer, [], seed=17)
    assert skipped["status"] == "skipped"
    assert skipped["optimizer_steps"] == 0
    assert skipped["exposure_sha256"] == semantic_digest([])
    evaluation = _evaluate(model, [], torch.empty((0, 1024)), model,
        [0, 0], check_index=1)
    assert evaluation["status"] == "insufficient_validation_data"
    assert evaluation["validation_signal"] is False
    assert evaluation["current"] is None


def test_v4_curve_updates_exactly_the_value_head_parameter_budget():
    model = initialize_fresh_model(20261103)
    assert model.schema == "sspi-policy-value.v4"
    assert _check_head_scope(model) == 262_914


def test_curve_cached_features_match_full_forward_and_still_allow_head_gradients():
    from native_runner.tests.test_sspi_search_performance import _real_information
    from native_runner.training.sspi.value_learning import encode_value_head_inputs
    torch.set_num_threads(1)
    model = initialize_fresh_model(20261103).eval()
    optimizer = create_value_optimizer(model, head_only=True)
    frozen = policy_function_sha(model)
    info = _real_information()
    rows = [ValueRow(f"cache-{index}", info, WLLabel(index % 2)) for index in range(5)]
    progress = []
    features = encode_value_head_inputs(model, rows,
        progress=lambda done, total: progress.append((done, total)))
    assert features.shape == (5, 1024)
    assert progress[-1] == (5, 5)
    with torch.no_grad():
        expected = torch.cat([model.value_only(row.information) for row in rows])
    torch.testing.assert_close(model.value_head(features), expected, atol=1e-6, rtol=1e-5)
    model.value_head(features).sum().backward()
    assert all(p.grad is not None for p in model.value_head.parameters())
    assert all(p.grad is None for n, p in model.named_parameters() if not n.startswith("value_head."))
    optimizer.zero_grad(set_to_none=True)
    assert policy_function_sha(model) == frozen


def test_curve_recovers_checkpoint_before_result_without_repeating_exposure(tmp_path, monkeypatch):
    from native_runner.training.sspi import value_curve as curve
    torch.set_num_threads(1)
    model = initialize_fresh_model(20261103).eval()
    optimizer = create_value_optimizer(model, head_only=True)
    identity = dict(protocol=curve.RUN_SCHEMA, run_id="curve-transaction")
    checkpoints = tmp_path / "checkpoints"
    save_checkpoint(checkpoints / "initial.pt", model, optimizer,
        identity=identity, optimizer_steps=0,
        run_state=dict(training_games=0, training_label_counts=[0, 0],
            trained_game_ranges=[], optimizer_steps=0))
    shutil.copyfile(checkpoints / "initial.pt", checkpoints / "current.pt")
    config = dict(run_id="curve-transaction", source_sha256="test-source",
        checkpoint_identity=identity, initial_policy_function_sha=policy_function_sha(model))
    rows, features = _curve_rows("validation", 64)
    for index in range(1, 65):
        directory = tmp_path / "staging" / "training" / f"slot-{index:04d}"
        directory.mkdir(parents=True)
        (directory / "episode.json").write_text(json.dumps(dict(index=index)))

    def episode(directory, config, split, index):
        return [ValueRow(f"train-{index:04d}", None, WLLabel(index % 2),
            owner=0, decision_index=0, fair_sha256="a" * 64)], dict(
                result=dict(winner=0, labels=["WIN", "LOSS"]))
    monkeypatch.setattr(curve, "_source_identity", lambda: "test-source")
    monkeypatch.setattr(curve, "_validate_episode", episode)
    monkeypatch.setattr(curve, "_load_split", lambda *a: (rows,
        dict(collected_games=2, decisive_games=2, selected_rows=64)))
    monkeypatch.setattr(curve, "encode_value_head_inputs", lambda model, rows, **kw:
        torch.ones((len(rows), 1024)))
    original_update = curve.train_value_curve_batch
    exposures = []
    def update(*args, **kwargs):
        exposures.append(len(args[2]))
        return original_update(*args, **kwargs)
    monkeypatch.setattr(curve, "train_value_curve_batch", update)
    atomic = curve._atomic_json
    def interrupted(path, record):
        if Path(path).parent.name == "checks":
            raise OSError("simulated interruption after checkpoint save")
        return atomic(path, record)
    monkeypatch.setattr(curve, "_atomic_json", interrupted)
    with pytest.raises(OSError, match="interruption"):
        curve._write_remote_training_batch(tmp_path, config, 1, 64, device="cpu")
    assert exposures == [64]
    assert (checkpoints / "curve-0064.pt").is_file()
    current_before = torch.load(checkpoints / "current.pt", weights_only=True)["payload"]
    assert current_before["run_state"]["training_games"] == 0
    monkeypatch.setattr(curve, "_atomic_json", atomic)
    record = curve._write_remote_training_batch(tmp_path, config, 1, 64, device="cpu")
    assert exposures == [64]
    assert record["current_batch"]["rows"] == 64
    assert record["checkpoint"]["optimizer_steps"] == 1
    current = torch.load(checkpoints / "current.pt", weights_only=True)["payload"]
    assert current["run_state"]["training_games"] == 64
    assert record["checkpoint"]["optimizer_sha256"] == current["optimizer_sha"]
    assert curve._write_remote_training_batch(tmp_path, config, 1, 64, device="cpu") == record
    assert curve._completed_checks(tmp_path, config) == [record]
    assert exposures == [64]
    (checkpoints / "curve-0064.pt").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="identity"):
        curve._write_remote_training_batch(tmp_path, config, 1, 64, device="cpu")


def test_curve_training_cli_rejects_cpu_instead_of_falling_back(tmp_path):
    from native_runner.training.sspi.value_curve import train_batch_cli
    with pytest.raises(RuntimeError, match="requires remote CUDA"):
        train_batch_cli(tmp_path, 1, 64, "cpu")


def test_curve_checkpoint_reader_rejects_old_schema_and_optimizer_tampering(tmp_path):
    from native_runner.training.sspi.value_curve import _read_checkpoint_payload
    model = initialize_fresh_model(20261103)
    optimizer = create_value_optimizer(model, head_only=True)
    identity = dict(protocol="curve-version-test")
    path = tmp_path / "version.pt"
    save_checkpoint(path, model, optimizer, identity=identity, optimizer_steps=0)
    original = torch.load(path, weights_only=True)
    envelope = dict(original, schema="sspi-mac-pilot-checkpoint.v3")
    torch.save(envelope, path)
    with pytest.raises(ValueError, match="checksum"):
        _read_checkpoint_payload(path, identity)
    payload = original["payload"]
    payload["optimizer"]["param_groups"][0]["lr"] = .1
    original["payload_sha"] = semantic_digest(payload)
    torch.save(original, path)
    with pytest.raises(ValueError, match="checksum"):
        _read_checkpoint_payload(path, identity)


def test_curve_completed_holdout_is_cached_and_bound_to_checkpoint(tmp_path, monkeypatch):
    from native_runner.training.sspi import value_curve as curve
    path = tmp_path / "checkpoints" / "current.pt"
    path.parent.mkdir()
    path.write_bytes(b"fixed-checkpoint")
    record = dict(schema=curve.HOLDOUT_SCHEMA, run_id="holdout-once",
        checkpoint_sha256=curve._file_sha(path), status="not_confirmed")
    curve._atomic_json(tmp_path / "final-holdout.json", record)
    monkeypatch.setattr(curve, "_load_checkpoint_model", lambda *a, **kw:
        pytest.fail("completed holdout must not evaluate again"))
    assert curve._remote_holdout(tmp_path, {"run_id": "holdout-once"}, device="cuda:0") == record
    path.write_bytes(b"different-checkpoint")
    with pytest.raises(ValueError, match="identity"):
        curve._remote_holdout(tmp_path, {"run_id": "holdout-once"}, device="cuda:0")


def test_curve_source_revisions_preserve_collected_episode_identity():
    from native_runner.training.sspi.value_curve import _episode_source
    config = dict(collector_source_sha="original", collector_source_revisions=[
        dict(split="training", start=65, end=1024, source_sha256="repaired"),
        dict(split="holdout", start=1, end=32, source_sha256="repaired")])
    assert _episode_source(config, "training", 64) == "original"
    assert _episode_source(config, "training", 65) == "repaired"
    assert _episode_source(config, "validation", 1) == "original"
    assert _episode_source(config, "holdout", 1) == "repaired"
    config["collector_source_revisions"].append(dict(split="training", start=64,
        end=65, source_sha256="ambiguous"))
    with pytest.raises(ValueError, match="overlap"):
        _episode_source(config, "training", 65)
