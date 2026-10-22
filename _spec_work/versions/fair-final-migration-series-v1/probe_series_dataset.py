import argparse
import hashlib
import json
from pathlib import Path

import torch

from series_training import SeriesMigrationDataset, implementation_sha
from native_runner.training.fair_final.storage import ensure_immutable, json_bytes
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--private-plan", type=Path, required=True)
    parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    plan = json.loads(args.private_plan.read_text())
    paths = [path for root in args.data_roots for path in root.rglob("*") if path.is_file()]
    before = {str(path): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths}
    teacher, _, source_sha = load_checkpoint(args.source, device="cpu",
        expected_sha256=plan["rounds"][0]["manifest"]["identity"]["source_a_sha"])
    teacher.eval().requires_grad_(False)
    teacher_sha = semantic_digest(teacher.state_dict())
    dataset = SeriesMigrationDataset(plan, args.data_roots, teacher=teacher)
    checked = []
    for number, store in enumerate(dataset.stores):
        for match_id, task in sorted(store.manifest["matches"].items()):
            episode, _ = store.load(match_id)
            row_id = episode["rows"][0]["row_id"]
            frame, legalities = dataset.batch([row_id], split=task["split"])
            if frame.batch_size != 1 or len(legalities) != 1:
                raise ValueError("series probe batch accounting differs")
            checked.append(dict(round=number, match_id=match_id, row_id=row_id, split=task["split"]))
    after = {str(path): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths}
    if before != after or semantic_digest(teacher.state_dict()) != teacher_sha:
        raise ValueError("series probe changed source data or frozen teacher")
    report = dict(status="engineering_native_series_batch_passed", source_sha256=source_sha,
        source_network_sha=teacher_sha, implementation_sha=implementation_sha(),
        probe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        series_sha=dataset.report["series_sha"], dataset_identity=dataset.identity,
        matches=dataset.report["matches"], rows=len(dataset.index),
        split_rows={name: len(rows) for name, rows in dataset.splits.items()},
        teacher_targets_recomputed_rows=len(checked), checked=checked, original_data_unchanged=True,
        teacher_unchanged=True, training_updates=0, training_admissible=False, formal_g2_pass=False)
    ensure_immutable(args.output, json_bytes(report))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
