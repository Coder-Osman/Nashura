"""Replay a completed warmup to audit all buffers; never reevaluate the holdout."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

import torch


def audit(run_dir, *, source_root=None):
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "firstlight_CR"))
    from native_runner.training.fair_search.contracts import semantic_digest
    from native_runner.training.sspi import tower_bootstrap as warmup
    from native_runner.training.sspi.augmentation import deserialize_information
    from native_runner.training.sspi.learning import load_checkpoint
    from native_runner.training.sspi.model import initialize_fresh_model
    from native_runner.training.sspi.tower_experiment import source_identity
    from native_runner.training.sspi.value_experiment import _atomic_json
    from native_runner.training.sspi.value_learning import policy_function_sha
    run_dir = Path(run_dir)
    report = json.loads((run_dir / "result.json").read_text())
    if report["status"] != "complete" or report["source_sha"] != source_identity():
        raise ValueError("audit requires the completed run's frozen implementation")
    best_path = run_dir / "checkpoints/best.pt"
    if hashlib.sha256(best_path.read_bytes()).hexdigest() != report["checkpoint"]["file_sha"]:
        raise ValueError("selected checkpoint checksum differs")
    torch.set_num_threads(1)
    model = initialize_fresh_model(warmup.SEED).eval()
    optimizer = warmup.create_optimizer(model)
    buffers = semantic_digest(dict(model.named_buffers()))
    state_keys = set(model.state_dict())
    nonpersistent_names = [name for name, _ in model.named_buffers() if name not in state_keys]
    initial_frozen = policy_function_sha(model)
    datasets, probes = {}, []
    for split in ("training", "validation"):
        caches = []
        for receipt in report["collections"][split]["receipts"]:
            path = run_dir / "cache" / (receipt["identity"]["game_id"] + ".pt")
            if hashlib.sha256(path.read_bytes()).hexdigest() != receipt["file_sha"]:
                raise ValueError("audit cache differs from original data")
            cache = torch.load(path, weights_only=True)
            caches.append(cache)
            if "policy_probe" in cache:
                probes.append(cache["policy_probe"])
        datasets[split] = dict(schema=warmup.CACHE_SCHEMA, shared_sha=initial_frozen,
            shared=torch.cat([c["shared"] for c in caches]),
            records=[r for c in caches for r in c["records"]])
    def check_buffers(_=None):
        if semantic_digest(dict(model.named_buffers())) != buffers:
            raise ValueError("a persistent or nonpersistent buffer changed")
    for probe in probes:
        if warmup.policy_signature(model, deserialize_information(probe["information"])) != probe["signature"]:
            raise ValueError("initial policy probe differs")
    check_buffers()
    update = warmup.train_head(model, optimizer, datasets["training"], datasets["validation"],
        checkpoint_path=run_dir / "audit/progress.pt",
        identity=dict(**report["checkpoint_identity"], audit="deterministic-replay-all-buffers.v1"),
        epoch_callback=check_buffers)
    check_buffers()
    reference = initialize_fresh_model(warmup.SEED).eval()
    reference_optimizer = warmup.create_optimizer(reference)
    load_checkpoint(best_path, reference, reference_optimizer,
        expected_identity=report["checkpoint_identity"])
    identical = semantic_digest((model.state_dict(), optimizer.state_dict())) == semantic_digest(
        (reference.state_dict(), reference_optimizer.state_dict()))
    if (not identical or update != report["update"] or policy_function_sha(model) != initial_frozen
            or semantic_digest(dict(reference.named_buffers())) != buffers):
        raise ValueError("replayed update/model/optimizer/buffers differ")
    for probe in probes:
        if warmup.policy_signature(model, deserialize_information(probe["information"])) != probe["signature"]:
            raise ValueError("replayed policy probe differs")
    check_buffers()
    result = dict(schema="sspi-tower-all-buffers-audit.v1", passed=True,
        model_sha=semantic_digest(model.state_dict()), optimizer_sha=semantic_digest(optimizer.state_dict()),
        all_buffers_sha=buffers, nonpersistent_buffer_count=len(nonpersistent_names),
        nonpersistent_buffer_names=nonpersistent_names, frozen_state_sha=initial_frozen,
        original_checkpoint_sha=report["checkpoint"]["file_sha"], update_identical=True,
        model_optimizer_identical=identical, all_buffers_unchanged=True,
        fixed_policy_probes=len(probes), policy_unchanged=True,
        heldout_evaluations=0, new_native_episodes=0, candidate_replaced=False)
    _atomic_json(run_dir / "all-buffers-replay-audit.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path,
        help="exact frozen source checkout for historical runs; never mix source identities")
    args = parser.parse_args()
    print(json.dumps(audit(args.run_dir, source_root=args.source_root), sort_keys=True))
