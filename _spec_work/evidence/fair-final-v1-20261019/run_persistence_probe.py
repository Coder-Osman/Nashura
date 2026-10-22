import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
import time

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2] / "firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.tests.test_fair_final_persistence import (advance_model, continuation_summary,
    fixture_bindings, fixture_model, ready_store, sha)
from native_runner.training.fair_final.checkpoint import export_candidate, save_checkpoint
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.replay import FrozenReplayCursor
from native_runner.training.fair_final.storage import EpisodeStore


def worker(*arguments, check=True):
    return subprocess.run([sys.executable, "-m", "native_runner.tests.test_fair_final_persistence", *arguments],
        cwd=SOURCE, capture_output=True, text=True, check=check, timeout=90)


def main():
    started = time.monotonic()
    output = ROOT / f"persistence-probe-{time.time_ns()}"
    output.mkdir()
    crashed = output / "publication"
    result = worker("crash", str(crashed), "unused", check=False)
    if result.returncode != 73 or (crashed / "completed").exists():
        raise ValueError("staging crash did not preserve an unpublished complete episode")
    staged = list((crashed / "staged").glob("*.pt.gz"))
    if len(staged) != 1:
        raise ValueError("ambiguous terminal recovery object")
    terminal_sha = staged[0].name.removesuffix(".pt.gz")
    published = json.loads(worker("publish", str(crashed), terminal_sha).stdout)
    duplicate = json.loads(worker("publish", str(crashed), terminal_sha).stdout)
    if published["status"] != "published" or duplicate["status"] != "duplicate":
        raise ValueError("restart publication was not idempotent")
    torch.set_num_threads(1)
    random.seed(20261019); np.random.seed(20261019); torch.manual_seed(20261019)
    store = ready_store(output / "store")
    sampler = FrozenReplayCursor(store, [sha("match")], seed=20261019, sample_budget=12)
    model, optimizer, _ = fixture_model()
    first_step = advance_model(model, optimizer, sampler)
    checkpoint = output / "learner.pt"
    checkpoint_identity = save_checkpoint(checkpoint, model, optimizer, sampler, bindings=fixture_bindings(),
        optimizer_steps=1, batch_size=2)
    baseline = continuation_summary(model, optimizer, sampler)
    child = worker("resume", str(output), checkpoint_identity["file_sha"])
    recovered = json.loads(child.stdout)
    if json.loads(json.dumps(baseline)) != recovered:
        raise ValueError("new-process continuation changed updates or random streams")
    candidate_identity = export_candidate(checkpoint, output / "candidate.pt",
        expected_sha=checkpoint_identity["file_sha"], bindings=fixture_bindings())
    if candidate_identity["network_sha"] != checkpoint_identity["network_sha"]:
        raise ValueError("candidate export changed network identity")
    sources = list((SOURCE / "native_runner/training/fair_final").glob("*.py"))
    sources.extend([SOURCE / "native_runner/tests/test_fair_final_persistence.py", Path(__file__)])
    hashes = {str(path.relative_to(ROOT.parents[2])): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    artifacts = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in output.rglob("*") if path.is_file()}
    report = dict(schema="fair-final-persistence-evidence.v1", status="passed",
        scope="synthetic contract fixtures; CPU FP32; batch=2; not production data or training",
        source_sha256=hashes, artifacts=artifacts, seconds=time.monotonic() - started,
        publication=dict(crash_exit=result.returncode, published=published, duplicate=duplicate,
            completed_matches=len(list((crashed / "completed").glob("*.json")))),
        checkpoint=checkpoint_identity, candidate=candidate_identity, before_checkpoint=first_step,
        uninterrupted=baseline, new_process=recovered, exact_continuation=True, optimizer_updates_after_restart=2,
        exclusions=["native in-progress battle restart", "CUDA update determinism",
            "real search-produced FULL128 labels", "migration soft-target data", "champion promotion"])
    atomic_json(output / "report.json", report)
    atomic_json(ROOT / "persistence-report.json", report)
    print(json.dumps(dict(status=report["status"], seconds=report["seconds"], output=str(output),
        exact_continuation=True, checkpoint=checkpoint_identity, candidate=candidate_identity), indent=2))


if __name__ == "__main__":
    main()
