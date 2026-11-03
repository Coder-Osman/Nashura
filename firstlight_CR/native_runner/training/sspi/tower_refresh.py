"""Collect fresh complete 32/8 tower-HP games on one existing local native actor."""

from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import traceback

import torch

from ..fair_search.contracts import semantic_digest
from ..v4.factory import production_semantic_bundle
from .collector import (_source_sha, _make_environment, EventBatchedSSPINative,
    preflight_native_endpoints, episode_seed_identity, VALUE_ARM)
from .learning import save_checkpoint
from .model import initialize_fresh_model
from .tower_bootstrap import SEED, TARGET_SCHEMA
from .tower_shared import DATA_SCHEMA, file_sha, create_optimizer
from .value_experiment import _atomic_json, _collect

SCHEMA = "sspi-tower-fresh-collection.v1"
SPLITS = {"training":32, "validation":8}


def planned_games(run_id, excluded=()):
    seen_match,seen_rollout = set(),set()
    for row in excluded:
        seen_match.add(row["actual_match_seed"])
        seen_rollout.add(row["actual_rollout_seed"])
    rows=[]
    for split,count in SPLITS.items():
        for index in range(1,count+1):
            game_id=f"tower-{split}-{index:04d}"
            identity=episode_seed_identity(SEED,run_id=run_id,arm=VALUE_ARM,game_id=game_id)
            if identity["actual_match_seed"] in seen_match or identity["actual_rollout_seed"] in seen_rollout:
                raise ValueError("fresh collection would repeat a native seed")
            seen_match.add(identity["actual_match_seed"])
            seen_rollout.add(identity["actual_rollout_seed"])
            rows.append(dict(split=split,game_id=game_id,**identity))
    return rows


def collect(*, run_dir, run_id, initial_model, actual_port=26889,
        belief_port=26893, belief_port_owner_1=26894, excluded_seeds=None):
    run_dir=Path(run_dir);run_dir.mkdir(parents=True,exist_ok=True)
    torch.set_num_threads(1)
    initial=torch.load(initial_model,weights_only=True,map_location="cpu")
    if initial.get("schema") != DATA_SCHEMA:
        raise ValueError("collector requires the original portable random initialization")
    model=initialize_fresh_model(SEED).eval()
    if initial["architecture"] != model.architecture:
        raise ValueError("fresh collector architecture differs")
    model.load_state_dict(initial["model"],strict=True)
    initial_sha=semantic_digest(model.state_dict())
    if initial_sha != initial["identity"]["initial_model_sha"]:
        raise ValueError("fresh collector random policy identity differs")
    excluded=json.loads(Path(excluded_seeds).read_text()) if excluded_seeds else []
    plan=planned_games(run_id,excluded)
    config_path=run_dir/"run_config.json"
    specification=dict(schema=SCHEMA,run_id=run_id,seed=SEED,splits=SPLITS,
        source_sha=file_sha(__file__),
        initial_model_sha=initial_sha,collector_source_sha=_source_sha(),
        initial_file_sha=file_sha(initial_model),planned_games=plan,
        excluded_seeds_sha=semantic_digest(excluded),sample_probability=.5,search_probability=0.,
        collection_policy="frozen-original-random",target_schema=TARGET_SCHEMA,
        native_ports=dict(actual=actual_port,belief_port=belief_port,belief_port_owner_1=belief_port_owner_1))
    if config_path.exists():
        config=json.loads(config_path.read_text())
        if any(config.get(k)!=v for k,v in specification.items()):
            raise ValueError("fresh collection identity differs")
        if file_sha(run_dir/"checkpoints/initial.pt") != config["initial_checkpoint"]["file_sha"]:
            raise ValueError("collector initial checkpoint differs")
    else:
        native=EventBatchedSSPINative("127.0.0.1",actual_port,timeout=90.,event_deltas=True)
        try:
            env=_make_environment(native,production_semantic_bundle())
            attest=preflight_native_endpoints("127.0.0.1",actual_port,
                {0:belief_port,1:belief_port_owner_1},transport="event-json-delta",
                ruleset_manifest=env.ruleset_manifest,include_belief=False)
        finally:
            native.close_transport()
        if (run_dir/"checkpoints/initial.pt").exists():
            raise RuntimeError("orphan collector initialization requires inspection")
        initial_receipt=save_checkpoint(run_dir/"checkpoints/initial.pt",model,create_optimizer(model),
            identity=specification,optimizer_steps=0)
        config=dict(**specification,engine_sha256=attest["actual"]["libg_sha256"],
            native_attestation=attest,initial_checkpoint=initial_receipt)
        _atomic_json(config_path,config)
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    collections={split:dict(receipts=[]) for split in SPLITS}
    for row in plan:
        split,game_id=row["split"],row["game_id"]
        status=dict(status="running",stage="collection",split=split,game_id=game_id,
            completed=len(collections[split]["receipts"]),target=SPLITS[split])
        _atomic_json(run_dir/"status.json",status);print(json.dumps(status),flush=True)
        directory,metadata=_collect(run_dir,config,game_id,split,model,
            actual_port=actual_port,belief_port=belief_port,belief_port_owner_1=belief_port_owner_1,
            transport="event-json-delta",tower_hp_targets=True)
        if metadata["identity"].get("heuristic_target_schema") != TARGET_SCHEMA:
            raise ValueError("missing tower target protocol")
        if any(metadata["identity"].get(k)!=v for k,v in row.items() if k not in ("split","game_id")):
            raise ValueError("actual collection seed differs from the frozen plan")
        receipt=dict(identity=dict(game_id=game_id,episode_sha=file_sha(directory/"episode.json")),
            actual_match_seed=row["actual_match_seed"],actual_rollout_seed=row["actual_rollout_seed"],
            samples=metadata["value_samples"],decisive=metadata["result"]["winner"] is not None,
            wall_seconds=metadata["elapsed_seconds"],search_roots=metadata["search_roots"])
        collections[split]["receipts"].append(receipt)
        _atomic_json(run_dir/f"{split}-collection.json",collections[split])
    for values in collections.values():
        receipts=values["receipts"]
        values.update(collected_games=len(receipts),decisive_games=sum(r["decisive"] for r in receipts),
            excluded_draw_games=sum(not r["decisive"] for r in receipts),samples=sum(r["samples"] for r in receipts))
    if semantic_digest(model.state_dict()) != initial_sha:
        raise ValueError("frozen collection model changed")
    report=dict(schema=SCHEMA,status="complete",run_id=run_id,collections=collections,
        initial_model_sha=initial_sha,original_holdout_used=False,search_roots=0)
    _atomic_json(run_dir/"result.json",report)
    _atomic_json(run_dir/"status.json",dict(status="complete",stage="fresh_data_ready",
        samples={s:v["samples"] for s,v in collections.items()}))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir",type=Path,required=True)
    parser.add_argument("--run-id",required=True)
    parser.add_argument("--initial-model",type=Path,required=True)
    parser.add_argument("--excluded-seeds",type=Path)
    parser.add_argument("--actual-port",type=int,default=26889)
    parser.add_argument("--belief-port",type=int,default=26893)
    parser.add_argument("--belief-port-owner-1",type=int,default=26894)
    args=vars(parser.parse_args());args["run_dir"].mkdir(parents=True,exist_ok=True)
    # The global per-endpoint lock also spans different fresh batch identities.
    lock_path=args["run_dir"].parent/f"actual-{args['actual_port']}.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            collect(**args)
        except BaseException as error:
            _atomic_json(args["run_dir"]/"status.json",dict(status="paused_failure",
                error=type(error).__name__,reason=str(error),traceback=traceback.format_exc()))
            raise


if __name__=="__main__":
    main()
