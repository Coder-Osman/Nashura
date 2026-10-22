"""Small deterministic tests; no checkpoint, engine, or full model required."""
from types import SimpleNamespace
import json

import pytest
import torch
import torch.nn.functional as F

from tools import eval_event_owner_contract as evaluator
from native_runner.training.event_model import EventTargetsV1


def logits(values, classes):
    return F.one_hot(torch.tensor(values), classes).float() * 4


def fixture():
    targets = EventTargetsV1(
        gate=torch.tensor([1, 1, 1, 0, 1]),
        card_row=torch.tensor([0, 1, 2, -1, -1]),
        delay_bin=torch.tensor([1, 1, 1, 0, 1]),
        cell=torch.tensor([0, 5, -1, -1, 0]),
        act_mask=torch.tensor([True, True, True, False, False]),
        cell_mask=torch.tensor([True, True, False, False, False]),
    )
    output = SimpleNamespace(
        gate_logits=logits([1, 0, 1, 0, 1], 2),
        card_logits=logits([0, 1, 2, 0, 0], 3),
        cell_logits=logits([5, 5, 0, 0, 0], 16),
        delay_logits=logits([2, 1, 1, 0, 1], 4),
    )
    return output, targets, [0, 1, 1, 0, 0]


def test_owner_denominators_gate_joint_and_invalid_cells():
    output, targets, owners = fixture()
    metrics = evaluator.OwnerMetrics(4)
    metrics.update(output, targets, owners)
    report = metrics.report()
    combined = report["combined"]
    assert combined["counts"]["act_total"] == 4  # includes missing-card ACT
    assert combined["counts"]["card_invalid"] == 1
    assert combined["counts"]["cell_total"] == 2
    assert combined["counts"]["cell_invalid"] == 2
    assert combined["accuracy"]["card_correct"] == .75
    assert combined["accuracy"]["cell_within1"] == 1
    assert combined["accuracy"]["joint_correct"] == .25
    assert report["owner0"]["accuracy"]["joint_correct"] == .5
    assert report["owner1"]["accuracy"]["joint_correct"] == 0
    assert combined["ce"]["gate"]["count"] == 5
    assert combined["ce"]["card"]["count"] == 3
    assert combined["ce"]["cell"]["count"] == 2
    assert combined["ce"]["delay"]["count"] == 4
    for key, count in combined["counts"].items():
        assert count == report["owner0"]["counts"][key] + report["owner1"]["counts"][key]


def test_sufficient_statistics_are_chunk_independent():
    output, targets, owners = fixture()
    full, split = evaluator.OwnerMetrics(4), evaluator.OwnerMetrics(4)
    full.update(output, targets, owners)
    for lo, hi in ((0, 1), (1, 3), (3, 5)):
        part = SimpleNamespace(**{k: v[lo:hi] for k, v in vars(output).items()})
        labels = EventTargetsV1(**{k: getattr(targets, k)[lo:hi] for k in targets.__slots__})
        split.update(part, labels, owners[lo:hi])
    for group, result in full.report().items():
        other = split.report()[group]
        assert result["counts"] == other["counts"]
        assert result["accuracy"] == other["accuracy"]
        for head, stats in result["ce"].items():
            assert stats == pytest.approx(other["ce"][head])


def test_modes_separate_and_zero_denominators():
    output, targets, owners = fixture()
    teacher, cascade = evaluator.OwnerMetrics(4), evaluator.OwnerMetrics(4)
    teacher.update(output, targets, owners)
    output.cell_logits = logits([15, 15, 15, 15, 15], 16)
    cascade.update(output, targets, owners)
    assert teacher.report()["combined"]["accuracy"]["joint_correct"] == .25
    assert cascade.report()["combined"]["accuracy"]["joint_correct"] == 0
    empty = evaluator.OwnerMetrics(4).report()
    assert empty["owner1"]["accuracy"]["joint_correct"] is None
    with pytest.raises(ValueError, match="owner"):
        teacher.update(output, targets, [0, 0, 0, 0, 2])


def test_inventory_checks_unselected_shards(tmp_path, monkeypatch):
    for name in ("a-shard-1.json", "z-shard-2.json"):
        (tmp_path / name).write_text("{}")
    calls = []
    def load(path, *, verify):
        assert verify
        calls.append(path.name)
        return SimpleNamespace(frames=1, records=[SimpleNamespace(owner=0)],
            summary={"frames": 1, "data_file": "x.crev.zst", "compressed_sha256": "hash"},
            manifest={"frames": 1, "contract": {"version": "bad" if path.name.startswith("z") else "good"}})
    def validate(contract):
        if contract["version"] == "bad":
            raise ValueError("legacy shard")
    monkeypatch.setattr(evaluator, "load_event_cache_shard", load)
    monkeypatch.setattr(evaluator, "validate_event_supervised_contract", validate)
    with pytest.raises(ValueError, match="legacy shard"):
        evaluator.validate_inventory(tmp_path)
    assert calls == ["a-shard-1.json", "z-shard-2.json"]


def test_atomic_report_and_nonfinite_rejection(tmp_path):
    path = tmp_path / "report.json"
    evaluator.atomic_report(path, {"test": 1})
    evaluator.atomic_report(path, {"test": 2})
    assert json.loads(path.read_text()) == {"test": 2}
    with pytest.raises(ValueError):
        evaluator.atomic_report(path, {"test": float("nan")})
    assert json.loads(path.read_text()) == {"test": 2}
    assert list(tmp_path.iterdir()) == [path]


def test_evaluate_reuses_encoding_and_runs_autoregressive_heads(tmp_path, monkeypatch):
    from dataclasses import dataclass
    @dataclass
    class Config:
        board_width: int = 4
        snapshot_only: bool = True
    output, targets, owners = fixture()
    calls = []
    class Batch:
        def narrow_batch(self, offset, length):
            assert (offset, length) == (0, 5)
            return self
        def to_model_input(self, *args, **kwargs):
            return self
    class Model:
        event_config = config = Config()
        backbone = SimpleNamespace(config=Config(), catalog=None)
        def eval(self):
            return self
        def initial_state(self, *args, **kwargs):
            return None
        def encode(self, *args, **kwargs):
            calls.append("encode")
            return "context"
        def heads(self, context, *, card_row, cell):
            assert context == "context"
            calls.append((card_row, cell))
            return output
    path = tmp_path / "a-shard-1.json"
    path.write_text("{}")
    inventory = [{"summary_file": path.name, "summary_sha256": evaluator.hashlib.sha256(path.read_bytes()).hexdigest()}]
    monkeypatch.setattr(evaluator, "validate_inventory", lambda root: inventory)
    def load_checkpoint(path, **kwargs):
        assert kwargs["initialization_only"] is True
        return SimpleNamespace(model=Model(), payload={}, checkpoint_sha256="weights", contract_sha256="config")
    monkeypatch.setattr(evaluator, "load_event_checkpoint", load_checkpoint)
    monkeypatch.setattr(evaluator, "load_event_cache_shard", lambda *args, **kwargs: SimpleNamespace(manifest={"contract": {}}))
    monkeypatch.setattr(evaluator, "validate_event_supervised_contract", lambda contract: None)
    monkeypatch.setattr(evaluator, "shard_batch", lambda shards: (Batch(), [SimpleNamespace(owner=o) for o in owners]))
    monkeypatch.setattr(evaluator, "build_event_targets", lambda *args, **kwargs: targets)
    args = SimpleNamespace(cache_root=tmp_path, checkpoint=tmp_path / "model.pt", seed=0, samples=256,
                           batch_shards=1, chunk_decisions=64, device="cpu", initialization_only=True,
                           legacy_snapshot_only=None, legacy_model_scale=None)
    report = evaluator.evaluate(args)
    assert calls[0] == "encode"
    assert calls[1][0] is targets.card_row
    assert calls[1][1].tolist() == [0, 5, 0, 0, 0]
    assert calls[2] == (None, None)
    assert len(calls) == 3
    assert report["checkpoint_sha256"] == "weights"
    assert report["heldout"]["selected_sha256"] == evaluator.digest(inventory)
    assert report["metrics"]["free_running"]["combined"]["counts"]["rows"] == 5


def test_cli_strict_defaults_and_initialization_opt_in(tmp_path, monkeypatch):
    captured = []
    def evaluate(args):
        captured.append(args)
        return {"heldout": {"selected_sha256": "abc"}}
    monkeypatch.setattr(evaluator, "evaluate", evaluate)
    argv = ["--cache-root", str(tmp_path), "--checkpoint", "old.pt", "--output", str(tmp_path / "report.json")]
    assert evaluator.main(argv) == 0
    assert captured[-1].initialization_only is False
    assert captured[-1].batch_shards == 1
    assert evaluator.main(argv + ["--initialization-only", "--legacy-snapshot-only", "true"]) == 0
    assert captured[-1].initialization_only is True
    with pytest.raises(SystemExit):
        evaluator.main(argv + ["--samples", "0"])
    with pytest.raises(SystemExit):
        evaluator.main(argv + ["--legacy-snapshot-only", "true"])
