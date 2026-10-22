import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.training.fair_final.contracts import exact_fields
from native_runner.training.fair_final.model import ConditionalPolicyValue
from native_runner.training.fair_final.storage import durable_write, json_bytes
from native_runner.training.fair_migration import MigrationLossWeights
from native_runner.training.fair_migration_data import MigrationDataset, MigrationStore
from native_runner.training.fair_migration_trainer import SCHEMA
from native_runner.training.fair_search.contracts import require_sha, semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.v4.config import ModelConfigV4
from tools.fair_migration_diagnostics import diagnose_validation


def load_diagnostic_student(path, *, expected_sha, dataset, device, bundle=None):
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != require_sha(expected_sha):
        raise ValueError("diagnostic migration checkpoint file checksum differs")
    envelope = torch.load(io.BytesIO(data), weights_only=True, map_location="cpu")
    exact_fields(envelope, {"schema", "sha256", "payload"}, "diagnostic migration envelope")
    payload = envelope["payload"]
    if envelope["schema"] != SCHEMA or semantic_digest(payload) != envelope["sha256"]:
        raise ValueError("diagnostic migration checkpoint semantic checksum differs")
    exact_fields(payload, {"schema", "identity", "splits", "teacher_network_sha", "architecture", "weights",
        "network", "optimizer", "rng", "cursor"}, "diagnostic migration payload")
    if (payload["schema"] != SCHEMA or payload["identity"] != dataset.identity or payload["splits"] != dataset.splits
            or payload["teacher_network_sha"] != semantic_digest(dataset.teacher.state_dict())):
        raise ValueError("diagnostic checkpoint teacher or whole-game data identity differs")
    MigrationLossWeights(**payload["weights"])
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=ModelConfigV4(**payload["architecture"]["encoder"]))
    if payload["architecture"] != student.architecture:
        raise ValueError("diagnostic checkpoint architecture differs")
    expected = student.state_dict()
    if (set(expected) != set(payload["network"]) or any(
            expected[name].shape != value.shape or expected[name].dtype != value.dtype
            or value.is_floating_point() and not bool(torch.isfinite(value).all())
            for name, value in payload["network"].items())):
        raise ValueError("diagnostic network tensor shape, dtype or value differs")
    student.load_state_dict(payload["network"], strict=True)
    return student.to(device).eval()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--checkpoint-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--engineering-acceptance", action="store_true", required=True)
    args = parser.parse_args()
    started = time.monotonic()
    if args.output.exists():
        raise ValueError("diagnostic output already exists")
    torch.set_num_threads(1)
    manifest = json.loads(args.manifest.read_text())
    teacher, _, source_sha = load_checkpoint(args.source, device=args.device,
        expected_sha256=manifest["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    store = MigrationStore(args.data, manifest=manifest)
    dataset = MigrationDataset(store, teacher=teacher)
    student = load_diagnostic_student(args.checkpoint, expected_sha=args.checkpoint_sha256,
        dataset=dataset, device=args.device)
    report = diagnose_validation(dataset, student, max_rows=args.max_rows)
    project = Path(__file__).resolve().parents[1]
    dependencies = [path for path in (project / "native_runner").rglob("*.py")
        if "tests" not in path.relative_to(project).parts]
    dependencies += [Path(__file__).resolve(), project / "tools/fair_migration_diagnostics.py"]
    report.update(source_checkpoint_sha256=source_sha, checkpoint_sha256=args.checkpoint_sha256,
        source_sha256={str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(dependencies)}, seconds=time.monotonic() - started,
        engineering_only=True, loader_semantics="read-only network evaluation; no optimizer or RNG resume",
        pytorch_version=str(torch.__version__), device=str(next(student.parameters()).device))
    durable_write(args.output, json_bytes(report))
    metrics = report["overall"]["metrics"]
    print(json.dumps(dict(status="completed", rows=report["overall"]["rows"],
        complete_validation=report["complete_validation"], seconds=report["seconds"],
        metrics={name: metrics[name]["episode_equal"] for name in (
            "exact_action_match", "teacher_act_exact_match", "mode_match", "wdl_teacher_kl", "value_teacher_mae")},
        training_admissible=False)), flush=True)


if __name__ == "__main__":
    main()
