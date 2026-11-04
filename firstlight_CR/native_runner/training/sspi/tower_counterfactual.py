"""Two-stage synthetic and real-context tower-HP fitting without new games."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from itertools import product
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import statistics
import time
import traceback

import torch

from ..fair_search.contracts import semantic_digest
from ..v4.tensors import concatenate_padded_tensor_records
from .augmentation import deserialize_information
from .collector import VALUE_EPISODE_SCHEMA
from .learning import CHECKPOINT_SCHEMA, load_checkpoint, save_checkpoint
from .model import initialize_fresh_model, _hand_features
from .tower_bootstrap import (FORMULA, SEED, TARGET_SCHEMA, score_metrics, soft_loss,
    tower_target, validate_target)
from .value_experiment import _atomic_json
from .value_learning import (configure_value_training, load_value_episode,
    policy_function_sha, policy_head_sha)


RUN_SCHEMA = "sspi-tower-counterfactual-two-stage.v1"
DATA_SCHEMA = "sspi-tower-counterfactual-index.v1"
AUGMENT_SCHEMA = "sspi-tower-counterfactual-episode.v1"
SPLIT_SEED_XOR = 0x544F5745
VARIANT_SEED_XOR = 0x434F554E
SPLIT_RATIOS = (0.8, 0.1, 0.1)
SPLIT_NAMES = ("training", "validation", "heldout")
TOWER_KINDS = ("king", "princess_left", "princess_right")
TOWER_KEYS = tuple((owner, kind) for owner in (0, 1) for kind in TOWER_KINDS)
TOWER_TYPE = {"king": 0, "princess_left": 1, "princess_right": 2}
TOWER_KIND = {value: key for key, value in TOWER_TYPE.items()}
HP_FEATURE_RATIO = 0
HP_FEATURE_ABSOLUTE = 5
HP_FEATURE_MAX_ABSOLUTE = 6
SYNTH_SCORE_BINS = 41
SYNTH_TEMPLATES_PER_SPLIT = 8
SYNTH_DAMAGE_LEVELS = (0.25, 0.6, 1.0)
SYNTH_COMBINATIONS = tuple(product(SYNTH_DAMAGE_LEVELS, repeat=6))
BATCH = 256
MICROBATCH = 16
LR = 1e-4
WEIGHT_DECAY = 1e-4
MAX_EPOCHS = 8
GRADIENT_CLIP = 1.0
BOOTSTRAP_REPLICATES = 2000


@dataclass(frozen=True)
class TowerState:
    source_id: str
    decision_index: int
    information: object
    tower_target: dict
    variants: tuple[dict, ...]


@dataclass(frozen=True)
class Example:
    information: object
    tower_target: dict
    hp_by_key: tuple[float, ...] | None
    source_tower_record: dict
    game_id: str
    state_id: str


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    temporary = path.with_name(path.name + f".{time.time_ns()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)
    return hashlib.sha256(content).hexdigest()


def source_sha():
    root = Path(__file__).resolve().parents[1]
    names = ("tower_counterfactual.py", "tower_bootstrap.py", "value_learning.py",
        "augmentation.py", "model.py", "learning.py", "collector.py")
    return semantic_digest({name: file_sha(root / "sspi" / name) for name in names})


def split_sources(sources, *, seed=SEED):
    """Deterministic 80/10/10 split by complete (run, game) identity."""
    if type(seed) is not int or seed < 0 or not sources:
        raise ValueError("episode splitting requires sources and a nonnegative seed")
    ids = [item["source_id"] for item in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("source episode identities are not unique")
    count = len(sources)
    exact = [ratio * count for ratio in SPLIT_RATIOS]
    sizes = [math.floor(value) for value in exact]
    remainder = count - sum(sizes)
    for index in sorted(range(len(sizes)), key=lambda i: (-(exact[i] - sizes[i]), i))[:remainder]:
        sizes[index] += 1
    order = sorted(sources, key=lambda item: item["source_id"])
    random.Random(seed ^ SPLIT_SEED_XOR).shuffle(order)
    result, offset = {}, 0
    for name, size in zip(SPLIT_NAMES, sizes):
        for item in order[offset:offset + size]:
            result[item["source_id"]] = name
        offset += size
    if offset != count or set(result) != set(ids):
        raise AssertionError("episode split lost or duplicated a source")
    return result


def _relative_tower_key(information, row):
    owner_type = int(information.frame.towers.owner_type[0, row])
    tower_type = int(information.frame.towers.tower_type[0, row])
    if owner_type not in (0, 1) or tower_type not in TOWER_KIND:
        raise ValueError("FAIR tower row has an invalid owner or tower type")
    owner = information.owner if owner_type == 0 else 1 - information.owner
    kind = TOWER_KIND[tower_type]
    if information.owner == 1:
        kind = {"princess_left": "princess_right", "princess_right": "princess_left"}.get(kind, kind)
    return owner, kind


def tower_rows_by_key(information, target_record):
    towers = information.frame.towers
    rows = {}
    for row in towers.mask[0].nonzero().flatten().tolist():
        key = _relative_tower_key(information, row)
        if key in rows:
            raise ValueError("FAIR tower tensor repeats a fixed tower identity")
        rows[key] = row
    if set(rows) != set(TOWER_KEYS):
        raise ValueError("FAIR tower tensor does not contain all six fixed tower identities")
    target_by_key = {(item["owner"], item["tower_kind"]): item
        for item in target_record["towers"] if item.get("mask") is not False}
    if set(target_by_key) != set(TOWER_KEYS):
        raise ValueError("public tower target does not contain all six identities")
    features = towers.features
    for key in TOWER_KEYS:
        row = rows[key]
        target = target_by_key[key]
        hp, maximum = float(target["hitpoints"]), float(target["max_hitpoints"])
        ratio = float(features[0, row, HP_FEATURE_RATIO])
        absolute = float(features[0, row, HP_FEATURE_ABSOLUTE])
        max_absolute = float(features[0, row, HP_FEATURE_MAX_ABSOLUTE])
        if (abs(ratio - hp / maximum) > 1e-3
                or abs(absolute - hp / 5000.) > 3e-3
                or abs(max_absolute - maximum / 5000.) > 3e-3):
            raise ValueError("tower HP tensor fields differ from the FAIR target record")
    return rows, target_by_key


def replace_tower_hp(information, target_record, hp_by_key):
    """Change only HP ratio and absolute HP, preserving every other FAIR field."""
    if len(hp_by_key) != len(TOWER_KEYS):
        raise ValueError("tower HP intervention requires six canonical values")
    rows, target_by_key = tower_rows_by_key(information, target_record)
    features = information.frame.towers.features.clone()
    for index, key in enumerate(TOWER_KEYS):
        value = float(hp_by_key[index])
        target = target_by_key[key]
        maximum = float(target["max_hitpoints"])
        original = float(target["hitpoints"])
        if not math.isfinite(value) or not 0. <= value <= maximum:
            raise ValueError("counterfactual tower HP is outside its public maximum")
        if (original == 0. and value != 0.) or (original > 0. and value <= 0.):
            raise ValueError("counterfactual changed tower destruction status")
        row = rows[key]
        features[0, row, HP_FEATURE_RATIO] = value / maximum
        features[0, row, HP_FEATURE_ABSOLUTE] = value / 5000.
    return replace(information, frame=replace(information.frame,
        towers=replace(information.frame.towers, features=features)))


def _activation(information, rows, key):
    if key[1] != "king":
        return True
    return bool(float(information.frame.towers.features[0, rows[key], 4]) >= 0.5)


def _hp_bounds(information, target_record, *, synthetic=False):
    rows, by_key = tower_rows_by_key(information, target_record)
    bounds = {}
    for key in TOWER_KEYS:
        target = by_key[key]
        hp = float(target["hitpoints"])
        maximum = float(target["max_hitpoints"])
        if hp == 0.:
            low = high = 0.
        elif key[1] == "king" and not _activation(information, rows, key):
            if synthetic and abs(hp - maximum) > 1e-3:
                raise ValueError("unactivated synthetic king tower must remain at full HP")
            low = high = hp
        else:
            low = min(hp, max(1., maximum * 0.005))
            high = maximum
        bounds[key] = (low, high)
    return rows, by_key, bounds


def _score_range(bounds, perspective=0):
    if type(perspective) is not int or perspective not in (0, 1):
        raise ValueError("tower score perspective must be owner 0 or 1")
    totals = {}
    for side in (0, 1):
        minimum = sum(bounds[(side, kind)][0] for kind in TOWER_KINDS)
        maximum = sum(bounds[(side, kind)][1] for kind in TOWER_KINDS)
        if maximum <= 0. or minimum < 0.:
            raise ValueError("counterfactual requires at least one surviving tower per side")
        totals[side] = (minimum, maximum)
    low = (totals[0][0] - totals[1][1]) / (totals[0][0] + totals[1][1])
    high = (totals[0][1] - totals[1][0]) / (totals[0][1] + totals[1][0])
    if not math.isfinite(low + high) or low > high:
        raise ValueError("tower HP bounds have no feasible score interval")
    return (low, high) if perspective == 0 else (-high, -low)


def _allocate_total(keys, bounds, factors, total):
    lows = [bounds[key][0] for key in keys]
    highs = [bounds[key][1] for key in keys]
    weights = [max(1e-8, (highs[i] or 1.) * factors[i]) for i in range(len(keys))]
    minimum, maximum = sum(lows), sum(highs)
    if total < minimum - 1e-5 or total > maximum + 1e-5:
        raise ValueError("requested tower total is outside the feasible interval")
    total = min(maximum, max(minimum, total))

    def values(scale):
        return [min(highs[i], max(lows[i], scale * weights[i])) for i in range(len(keys))]

    left, right = 0., 1.
    while sum(values(right)) < total and right < 1e16:
        right *= 2.
    for _ in range(64):
        middle = (left + right) * 0.5
        if sum(values(middle)) < total:
            left = middle
        else:
            right = middle
    result = values((left + right) * 0.5)
    delta = total - sum(result)
    for index in range(len(result)):
        room = highs[index] - result[index] if delta > 0 else result[index] - lows[index]
        adjustment = math.copysign(min(abs(delta), max(0., room)), delta)
        result[index] += adjustment
        delta -= adjustment
        if abs(delta) <= 1e-7:
            break
    if (abs(sum(result) - total) > 1e-4
            or any(value <= 0. for key, value in zip(keys, result) if bounds[key][1] > 0.)):
        raise ValueError("tower HP allocation failed its bounds")
    return result


def hp_values_for_score(information, target_record, score, factors, *, synthetic=False):
    """Allocate per-tower HP values whose aggregate exactly matches a target score."""
    if type(score) not in (int, float) or not math.isfinite(score) or not -1. < score < 1.:
        raise ValueError("tower score target must be finite and strictly between -1 and 1")
    if len(factors) != len(TOWER_KEYS) or any(not math.isfinite(x) or x <= 0 for x in factors):
        raise ValueError("tower damage factors must contain six positive values")
    _rows, _targets, bounds = _hp_bounds(information, target_record, synthetic=synthetic)
    low, high = _score_range(bounds, information.owner)
    tolerance = 1e-8
    if score < low - tolerance or score > high + tolerance:
        raise ValueError("requested score is outside this scene's feasible tower HP range")
    score = min(high, max(low, float(score)))
    side_zero_score = score if information.owner == 0 else -score
    own_low = sum(bounds[(0, kind)][0] for kind in TOWER_KINDS)
    own_high = sum(bounds[(0, kind)][1] for kind in TOWER_KINDS)
    enemy_low = sum(bounds[(1, kind)][0] for kind in TOWER_KINDS)
    enemy_high = sum(bounds[(1, kind)][1] for kind in TOWER_KINDS)
    lower_scale = max(own_low / (1. + side_zero_score), enemy_low / (1. - side_zero_score))
    upper_scale = min(own_high / (1. + side_zero_score), enemy_high / (1. - side_zero_score))
    if lower_scale > upper_scale + 1e-5:
        raise ValueError("score and tower survival constraints have no common total")
    scale = (lower_scale + upper_scale) * 0.5
    desired = {0: scale * (1. + side_zero_score), 1: scale * (1. - side_zero_score)}
    result = [0.] * len(TOWER_KEYS)
    for owner in (0, 1):
        keys = [key for key in TOWER_KEYS if key[0] == owner]
        indexes = [TOWER_KEYS.index(key) for key in keys]
        allocation = _allocate_total(keys, bounds, [factors[i] for i in indexes], desired[owner])
        for key, value in zip(keys, allocation):
            result[TOWER_KEYS.index(key)] = value
    actual_own = sum(result[TOWER_KEYS.index((information.owner, kind))] for kind in TOWER_KINDS)
    actual_enemy = sum(result[TOWER_KEYS.index((1 - information.owner, kind))] for kind in TOWER_KINDS)
    actual_score = (actual_own - actual_enemy) / (actual_own + actual_enemy)
    if abs(actual_score - score) > 1e-5:
        raise ValueError("allocated HP does not match the requested tower score")
    return result, actual_score


def _target_from_hp(target_record, owner, hp_values):
    updated = []
    by_key = {(item["owner"], item["tower_kind"]): item for item in target_record["towers"]}
    for index, key in enumerate(TOWER_KEYS):
        row = dict(by_key[key], hitpoints=float(hp_values[index]))
        updated.append(row)
    target = tower_target(updated, owner)
    return dict(tower_hp=tuple(float(value) for value in hp_values),
        score=target["score"], soft_target=tuple(target["soft_target"]))


def _variant_seed(source_id, owner, decision_index, variant):
    raw = f"{SEED ^ VARIANT_SEED_XOR}:{source_id}:{owner}:{decision_index}:{variant}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def _episode_epoch_seed(source_id, epoch):
    raw = f"{SEED ^ VARIANT_SEED_XOR}:episode-order:{source_id}:{epoch}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def counterfactual_variants(information, target_record, source_id, decision_index, *, synthetic=False):
    _rows, _targets, bounds = _hp_bounds(information, target_record, synthetic=synthetic)
    low, high = _score_range(bounds, information.owner)
    variants = []
    for index, quantile in enumerate((0.1, 0.5, 0.9), start=1):
        score = low + (high - low) * quantile
        rng = random.Random(_variant_seed(source_id, information.owner, decision_index, index))
        factors = [rng.uniform(0.25, 1.75) for _ in TOWER_KEYS]
        hp_values, actual_score = hp_values_for_score(information, target_record, score, factors,
            synthetic=synthetic)
        variant = _target_from_hp(target_record, information.owner, hp_values)
        if abs(variant["score"] - actual_score) > 1e-8:
            raise ValueError("counterfactual target and allocated HP differ")
        variants.append(variant)
    return tuple(variants)


def validate_variant(information, target_record, variant, *, synthetic=False):
    hp_values = variant.get("tower_hp")
    if not isinstance(hp_values, (list, tuple)) or len(hp_values) != len(TOWER_KEYS):
        raise ValueError("counterfactual record has no six-tower HP vector")
    target = _target_from_hp(target_record, information.owner, hp_values)
    if (not math.isclose(float(variant.get("score", math.nan)), target["score"], abs_tol=1e-8)
            or list(variant.get("soft_target", ())) != list(target["soft_target"])):
        raise ValueError("counterfactual score or soft label checksum differs")
    hp_values_for_score(information, target_record, target["score"],
        [1.] * len(TOWER_KEYS), synthetic=synthetic)  # Revalidates survival and feasibility.
    bounds = _hp_bounds(information, target_record, synthetic=synthetic)[2]
    for index, key in enumerate(TOWER_KEYS):
        original = next(row for row in target_record["towers"]
            if (row["owner"], row["tower_kind"]) == key)["hitpoints"]
        if (float(original) == 0.) != (float(hp_values[index]) == 0.):
            raise ValueError("counterfactual changed tower destruction status")
        low, high = bounds[key]
        if not low - 1e-5 <= float(hp_values[index]) <= high + 1e-5:
            raise ValueError("counterfactual violates a tower's fixed-state HP bounds")
    return target


def _is_minimal_scene(information, target_record):
    frame = information.frame
    return (not bool(frame.groups.mask.any())
        and not bool(frame.active_effects.mask.any())
        and not bool(frame.events.mask.any())
        and all(float(row["hitpoints"]) > 0. for row in target_record["towers"]))


def _identity_for_source_run(run_dir, expected_run_id=None):
    run_dir = Path(run_dir)
    config = json.loads((run_dir / "run_config.json").read_text())
    result = json.loads((run_dir / "result.json").read_text())
    run_id = config.get("run_id")
    if (not run_id or (expected_run_id is not None and run_id != expected_run_id)
            or result.get("run_id") != run_id or result.get("status") != "complete"
            or result.get("search_roots") != 0 or config.get("seed") != SEED
            or config.get("search_probability") != 0.
            or config.get("sample_probability") != 0.5
            or config.get("initial_model_sha") != "14fbd5fa43ba84be860f4333f3c63a935879b2ca0d6221978100c93cb6061415"
            or config.get("target_schema") != TARGET_SCHEMA):
        raise ValueError("source run is not a complete random-policy, zero-search tower run")
    training = result.get("collections", {}).get("training", {})
    receipts = training.get("receipts")
    if not isinstance(receipts, list) or len(receipts) != 32:
        raise ValueError("source run must have exactly 32 complete training receipts")
    sources = []
    for receipt in receipts:
        identity = receipt.get("identity") or {}
        game_id = identity.get("game_id")
        source_id = f"{run_id}/{game_id}"
        directory = run_dir / "episodes" / game_id
        metadata_path = directory / "episode.json"
        metadata_sha = file_sha(metadata_path)
        if (not game_id or identity.get("episode_sha") != metadata_sha
                or receipt.get("decisive") is not True or receipt.get("search_roots") != 0
                or type(receipt.get("samples")) is not int or receipt["samples"] < 1):
            raise ValueError("source receipt is incomplete, searched, or nondeterministic")
        metadata = json.loads(metadata_path.read_text())
        episode_identity = metadata.get("identity") or {}
        if (metadata.get("schema") != VALUE_EPISODE_SCHEMA or metadata.get("status") != "complete"
                or metadata.get("episode_kind") != "training" or metadata.get("training_admissible") is not True
                or metadata.get("search_roots") != 0 or metadata.get("search_probability") != 0.
                or metadata.get("value_sample_probability") != 0.5
                or episode_identity.get("run_id") != run_id or episode_identity.get("game_id") != game_id
                or episode_identity.get("model_sha256") != result["initial_model_sha"]
                or metadata.get("value_samples") != receipt["samples"]
                or (metadata.get("result") or {}).get("winner") is None):
            raise ValueError("source episode is not an admissible natural decisive training game")
        match_seed = receipt.get("actual_match_seed")
        rollout_seed = receipt.get("actual_rollout_seed")
        if type(match_seed) is not int or type(rollout_seed) is not int:
            raise ValueError("source receipt lacks its native random seed identity")
        sources.append(dict(source_id=source_id, run_id=run_id, game_id=game_id,
            episode_dir=str(directory.resolve()), episode_sha=metadata_sha,
            samples=receipt["samples"], actual_match_seed=match_seed,
            actual_rollout_seed=rollout_seed, run_config_sha=file_sha(run_dir / "run_config.json"),
            result_sha=file_sha(run_dir / "result.json")))
    if len({item["source_id"] for item in sources}) != 32:
        raise ValueError("source run repeats a training game identity")
    return config, result, sources


def discover_sources(bootstrap_run, refresh_root):
    """Select only historical and batch-1..8 training episodes; never batch 9 or old holdout."""
    base_config, base_result, sources = _identity_for_source_run(bootstrap_run,
        "sspi-tower-bootstrap-20261103-v1")
    heldout_receipts = base_result.get("collections", {}).get("test", {}).get("receipts", [])
    old_test_match = {row.get("actual_match_seed") for row in heldout_receipts}
    old_test_rollout = {row.get("actual_rollout_seed") for row in heldout_receipts}
    refresh_root = Path(refresh_root)
    run_ids = []
    for batch in range(1, 9):
        run_dir = refresh_root / f"batch-{batch:04d}"
        config, _result, selected = _identity_for_source_run(run_dir,
            f"sspi-tower-refresh-20261103-v1-b{batch:04d}")
        if (config["initial_model_sha"] != base_config["initial_model_sha"]
                or config["seed"] != base_config["seed"]):
            raise ValueError("fresh batch random initialization differs from historical policy")
        run_ids.append(config["run_id"])
        sources.extend(selected)
    if len(sources) != 288 or len({item["source_id"] for item in sources}) != 288:
        raise ValueError("counterfactual input must contain exactly 288 unique training episodes")
    match_seeds = [item["actual_match_seed"] for item in sources]
    rollout_seeds = [item["actual_rollout_seed"] for item in sources]
    if (len(set(match_seeds)) != len(match_seeds) or len(set(rollout_seeds)) != len(rollout_seeds)
            or set(match_seeds) & old_test_match or set(rollout_seeds) & old_test_rollout):
        raise ValueError("training source seeds overlap each other or the previously evaluated holdout")
    assignment = split_sources(sources)
    for source in sources:
        source["split"] = assignment[source["source_id"]]
    sources.sort(key=lambda item: item["source_id"])
    return dict(schema=DATA_SCHEMA, seed=SEED, source_run_ids=[base_config["run_id"], *run_ids],
        initial_model_sha=base_config["initial_model_sha"], source_sha=source_sha(),
        split_sizes={name: sum(item["split"] == name for item in sources) for name in SPLIT_NAMES},
        sources=sources)


def _augmentation_path(data_dir, source_id):
    token = hashlib.sha256(source_id.encode()).hexdigest()
    return Path(data_dir) / "counterfactuals" / f"{token}.json"


def _counterfactual_episode(source, data_dir, template_banks):
    directory = Path(source["episode_dir"])
    if file_sha(directory / "episode.json") != source["episode_sha"]:
        raise ValueError("source episode metadata checksum changed")
    rows, metadata, payload = load_value_episode(directory, expected_kind="training",
        run_id=source["run_id"], model_sha=source["model_sha"] if "model_sha" in source else
            "14fbd5fa43ba84be860f4333f3c63a935879b2ca0d6221978100c93cb6061415", include_raw=True)
    raw_rows = payload.get("value_rows") or []
    if len(rows) != len(raw_rows) or len(rows) != source["samples"]:
        raise ValueError("source episode sample count changed during data preparation")
    records = []
    seen = set()
    excluded = 0
    for row, raw in zip(rows, raw_rows):
        target = raw.get("tower_hp_target") or {}
        if not validate_target(target, raw):
            excluded += 1
            continue
        decision = raw["decision_index"]
        state_identity = (row.information.owner, decision)
        if state_identity in seen:
            raise ValueError("source episode repeats a selected decision identity")
        seen.add(state_identity)
        tower_rows_by_key(row.information, target)
        variants = counterfactual_variants(row.information, target, source["source_id"], decision)
        records.append(dict(owner=row.information.owner, decision_index=decision,
            fair_sha256=raw["fair_sha256"],
            original_score=target["score"], original_soft_target=target["soft_target"],
            variants=[dict(tower_hp=list(variant["tower_hp"]), score=variant["score"],
                soft_target=list(variant["soft_target"])) for variant in variants]))
        bank = template_banks[source["split"]]
        if len(bank) < SYNTH_TEMPLATES_PER_SPLIT and _is_minimal_scene(row.information, target):
            try:
                _hp_bounds(row.information, target, synthetic=True)
            except ValueError:
                pass
            else:
                bank.append(dict(source_id=source["source_id"], game_id=source["source_id"],
                    owner=row.information.owner, decision_index=decision,
                    fair_sha256=raw["fair_sha256"]))
    augmentation = dict(schema=AUGMENT_SCHEMA, source_id=source["source_id"],
        episode_sha=source["episode_sha"], valid_states=len(records), excluded_states=excluded,
        records=records)
    return augmentation, metadata


def prepare_dataset(bootstrap_run, refresh_root, data_dir, run_id):
    """Split episodes first, then write small deterministic HP-only manifests."""
    data_dir = Path(data_dir)
    catalog = discover_sources(bootstrap_run, refresh_root)
    catalog_sha = semantic_digest(catalog)
    dataset_path = data_dir / "dataset.json"
    if dataset_path.exists():
        manifest = json.loads(dataset_path.read_text())
        identity = manifest.get("identity", {})
        if (identity.get("run_id") != run_id
                or not _manifest_catalog_matches(manifest, catalog)):
            raise ValueError("existing counterfactual dataset has a different identity")
        for entry in manifest["augmentation_files"]:
            if file_sha(data_dir / entry["path"]) != entry["sha256"]:
                raise ValueError("counterfactual augmentation file checksum differs")
        return manifest
    data_dir.mkdir(parents=True, exist_ok=True)
    template_banks = {"training": [], "validation": [], "heldout": []}
    source_entries, augmentation_files = [], []
    for source in catalog["sources"]:
        entry = dict(source)
        if source["split"] == "heldout":
            source_entries.append(entry)
            continue
        augmentation, metadata = _counterfactual_episode(source, data_dir, template_banks)
        path = _augmentation_path(data_dir, source["source_id"])
        if path.exists():
            existing = json.loads(path.read_text())
            if semantic_digest(existing) != semantic_digest(augmentation):
                raise ValueError("partial counterfactual manifest differs from deterministic rebuild")
            checksum = file_sha(path)
        else:
            checksum = _write_json(path, augmentation)
        item = dict(path=str(path.relative_to(data_dir)), sha256=checksum,
            samples=augmentation["valid_states"], excluded=augmentation["excluded_states"])
        entry["augmentation"] = item
        source_entries.append(entry)
        augmentation_files.append(dict(source_id=source["source_id"], **item))
        print(json.dumps(dict(stage="counterfactual-index", source_id=source["source_id"],
            samples=item["samples"], excluded=item["excluded"]), sort_keys=True), flush=True)
    if (len(template_banks["training"]) < SYNTH_TEMPLATES_PER_SPLIT
            or len(template_banks["validation"]) < SYNTH_TEMPLATES_PER_SPLIT):
        raise ValueError("not enough distinct empty-scene templates in the episode-disjoint splits")
    totals = {name: dict(episodes=sum(item["split"] == name for item in source_entries),
        states=sum(item.get("augmentation", {}).get("samples", 0)
            for item in source_entries if item["split"] == name),
        excluded=sum(item.get("augmentation", {}).get("excluded", 0)
            for item in source_entries if item["split"] == name)) for name in SPLIT_NAMES}
    identity = dict(schema=DATA_SCHEMA, run_id=run_id, seed=SEED, catalog_sha=catalog_sha,
        source_sha=source_sha(), initial_model_sha=catalog["initial_model_sha"],
        split_sizes=catalog["split_sizes"], split_ratios=list(SPLIT_RATIOS),
        variant_count=4, deterministic_variant_seed_xor=VARIANT_SEED_XOR,
        source_runs=catalog["source_run_ids"],
        synthetic=dict(score_bins=SYNTH_SCORE_BINS, damage_levels=list(SYNTH_DAMAGE_LEVELS),
            template_count=SYNTH_TEMPLATES_PER_SPLIT, combination_count=len(SYNTH_COMBINATIONS)))
    manifest = dict(identity=identity, totals=totals, sources=source_entries,
        augmentation_files=augmentation_files, synthetic_templates=template_banks,
        heldout_policy="episode identity only until final paired initial/final inference")
    _write_json(dataset_path, manifest)
    return manifest


def _manifest_catalog_matches(manifest, catalog):
    """Allow trainer-only source edits while retaining the original generation identity."""
    identity = manifest.get("identity", {})
    sources = [{key: value for key, value in entry.items() if key != "augmentation"}
        for entry in manifest.get("sources", [])]
    recorded = dict(schema=DATA_SCHEMA, seed=identity.get("seed"),
        source_run_ids=identity.get("source_runs"),
        initial_model_sha=identity.get("initial_model_sha"),
        source_sha=identity.get("source_sha"), split_sizes=identity.get("split_sizes"),
        sources=sources)
    current = dict(catalog)
    recorded_sha = recorded.pop("source_sha")
    current_sha = current.pop("source_sha")
    recorded_catalog = dict(recorded, source_sha=recorded_sha)
    return (recorded_sha is not None and current_sha == source_sha()
        and semantic_digest(recorded_catalog) == identity.get("catalog_sha")
        and semantic_digest(recorded) == semantic_digest(current)
        and identity.get("schema") == DATA_SCHEMA)


def _load_augmentation(data_dir, entry):
    path = Path(data_dir) / entry["augmentation"]["path"]
    if file_sha(path) != entry["augmentation"]["sha256"]:
        raise ValueError("counterfactual per-episode checksum differs")
    result = json.loads(path.read_text())
    if (result.get("schema") != AUGMENT_SCHEMA or result.get("source_id") != entry["source_id"]
            or result.get("episode_sha") != entry["episode_sha"]):
        raise ValueError("counterfactual per-episode identity differs")
    return result


def load_episode_states(entry, *, model_sha, augmentation=None, validate_saved_variants=True):
    directory = Path(entry["episode_dir"])
    if file_sha(directory / "episode.json") != entry["episode_sha"]:
        raise ValueError("source episode metadata changed after split assignment")
    rows, metadata, payload = load_value_episode(directory, expected_kind="training",
        run_id=entry["run_id"], model_sha=model_sha, include_raw=True)
    raw_rows = payload.get("value_rows") or []
    if len(rows) != len(raw_rows) or len(rows) != entry["samples"]:
        raise ValueError("source episode row count differs from its complete receipt")
    by_decision = {}
    if augmentation is not None:
        by_decision = {(row["owner"], row["decision_index"]): row
            for row in augmentation["records"]}
        if len(by_decision) != len(augmentation["records"]):
            raise ValueError("counterfactual episode repeats a decision")
    result = []
    for row, raw in zip(rows, raw_rows):
        target = raw.get("tower_hp_target") or {}
        if not validate_target(target, raw):
            continue
        decision = raw["decision_index"]
        state_identity = (row.information.owner, decision)
        if augmentation is None:
            variants = counterfactual_variants(row.information, target, entry["source_id"], decision)
        else:
            saved = by_decision.pop(state_identity, None)
            if saved is None or saved.get("fair_sha256") != raw.get("fair_sha256"):
                raise ValueError("counterfactual state identity differs from the source FAIR row")
            if (not math.isclose(float(saved.get("original_score", math.nan)),
                    float(target["score"]), abs_tol=1e-8)
                    or saved.get("original_soft_target") != target["soft_target"]):
                raise ValueError("counterfactual original target differs from its source FAIR row")
            variants = tuple(validate_variant(row.information, target, item)
                for item in saved["variants"]) if validate_saved_variants else tuple(saved["variants"])
        result.append(TowerState(entry["source_id"], decision, row.information,
            target, variants))
    if by_decision:
        raise ValueError("counterfactual manifest references a missing source FAIR row")
    return result, metadata


def _features_with_hp(information, tower_target_record, hp_by_key):
    changed = replace_tower_hp(information, tower_target_record, hp_by_key)
    return changed


def _example_frame(example):
    return example.information.frame if example.hp_by_key is None else _features_with_hp(
        example.information, example.source_tower_record, example.hp_by_key).frame


def _batch_tensors(examples, device):
    frames = tuple(_example_frame(example) for example in examples)
    frame = concatenate_padded_tensor_records(frames)
    hands = torch.stack([_hand_features(example.information.hand, device="cpu") for example in examples])
    targets = torch.tensor([example.tower_target["soft_target"] for example in examples], dtype=torch.float32)
    return frame, hands, targets.to(device=device)


def _score_predictions(model, examples, *, device, batch_size=MICROBATCH):
    model.eval()
    predictions, records = [], []
    with torch.inference_mode():
        for start in range(0, len(examples), batch_size):
            batch = examples[start:start + batch_size]
            frame, hands, _targets = _batch_tensors(batch, device)
            probabilities = model.value_batch(frame, hands).softmax(-1)
            predictions.extend((probabilities[:, 0] - probabilities[:, 1]).cpu().tolist())
            records.extend(dict(game_id=item.game_id, score=item.tower_target["score"],
                state_id=item.state_id) for item in batch)
    return records, predictions


def _evaluate_real_split(model, entries, data_dir, model_sha, *, device, replicates=0):
    records, predictions = [], []
    for entry in entries:
        augmentation = _load_augmentation(data_dir, entry)
        states, _metadata = load_episode_states(entry, model_sha=model_sha,
            augmentation=augmentation, validate_saved_variants=False)
        examples = list(_real_examples(states))
        game_records, game_predictions = _score_predictions(model, examples, device=device)
        records.extend(game_records)
        predictions.extend(game_predictions)
    metrics = score_metrics(records, predictions, replicates=replicates)
    return metrics, records, predictions


def _make_optimizer(model):
    parameters = configure_value_training(model, head_only=False)
    if sum(parameter.numel() for parameter in parameters) != 7_055_045:
        raise ValueError("tower fit must update the shared body and value head only")
    if any(parameter.requires_grad for parameter in model.policy_head.parameters()):
        raise ValueError("policy head must stay outside the value optimizer")
    return torch.optim.AdamW(parameters, lr=LR, weight_decay=WEIGHT_DECAY,
        betas=(0.9, 0.999), eps=1e-8)


def _gradient_summary(model):
    prefixes = ("encoder", "hand_projection", "trunk", "value_head")
    values = {prefix: sum(float(parameter.grad.detach().abs().sum())
        for name, parameter in model.named_parameters()
        if name.startswith(prefix + ".") and parameter.grad is not None) for prefix in prefixes}
    if any(value <= 0 or not math.isfinite(value) for value in values.values()):
        raise ValueError("a shared/value module has no finite gradient")
    if any(parameter.grad is not None for parameter in model.policy_head.parameters()):
        raise ValueError("policy head received a tower value gradient")
    return values


def train_examples(model, optimizer, examples, *, epoch, device, seed_key):
    if not examples:
        raise ValueError("tower training epoch has no examples")
    order = list(range(len(examples)))
    random.Random(SEED ^ epoch ^ seed_key).shuffle(order)
    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    model.train()
    optimizer.zero_grad(set_to_none=True)
    total_ce, seen, steps = 0., 0, 0
    first_gradients = None
    max_norm = 0.
    for start in range(0, len(order), BATCH):
        indices = order[start:start + BATCH]
        optimizer.zero_grad(set_to_none=True)
        for offset in range(0, len(indices), MICROBATCH):
            selected = [examples[order_index] for order_index in indices[offset:offset + MICROBATCH]]
            frame, hands, targets = _batch_tensors(selected, device)
            logits = model.value_batch(frame, hands)
            loss = soft_loss(logits, targets)
            (loss * len(selected) / len(indices)).backward()
            total_ce += float(loss.detach()) * len(selected)
            seen += len(selected)
        if first_gradients is None:
            first_gradients = _gradient_summary(model)
        norm = torch.nn.utils.clip_grad_norm_(parameters, GRADIENT_CLIP)
        if not bool(torch.isfinite(norm)):
            raise ValueError("nonfinite tower gradient norm")
        max_norm = max(max_norm, float(norm))
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        steps += 1
    return dict(epoch=epoch, samples=seen, steps=steps, soft_ce=total_ce / seen,
        max_unclipped_gradient_norm=max_norm, first_batch_gradient_l1=first_gradients,
        ordering_sha=semantic_digest(order))


def train_real_epoch(model, optimizer, entries, data_dir, model_sha, *, epoch, device):
    order = list(entries)
    random.Random(SEED ^ epoch ^ 0x5245414C).shuffle(order)
    buffer, total_ce, seen, steps = [], 0., 0, 0
    parameters = [parameter for parameter in model.parameters() if parameter.requires_grad]
    model.train()
    first_gradients, max_norm = None, 0.

    def update(batch):
        nonlocal total_ce, seen, steps, first_gradients, max_norm
        optimizer.zero_grad(set_to_none=True)
        for offset in range(0, len(batch), MICROBATCH):
            selected = batch[offset:offset + MICROBATCH]
            frame, hands, targets = _batch_tensors(selected, device)
            logits = model.value_batch(frame, hands)
            loss = soft_loss(logits, targets)
            (loss * len(selected) / len(batch)).backward()
            total_ce += float(loss.detach()) * len(selected)
            seen += len(selected)
        if first_gradients is None:
            first_gradients = _gradient_summary(model)
        norm = torch.nn.utils.clip_grad_norm_(parameters, GRADIENT_CLIP)
        if not bool(torch.isfinite(norm)):
            raise ValueError("nonfinite real-context gradient norm")
        max_norm = max(max_norm, float(norm))
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        steps += 1

    for entry in order:
        augmentation = _load_augmentation(data_dir, entry)
        states, _metadata = load_episode_states(entry, model_sha=model_sha,
            augmentation=augmentation, validate_saved_variants=False)
        local = list(_real_examples(states))
        random.Random(_episode_epoch_seed(entry["source_id"], epoch)).shuffle(local)
        buffer.extend(local)
        while len(buffer) >= BATCH:
            update(buffer[:BATCH])
            del buffer[:BATCH]
    if buffer:
        update(buffer)
    if not seen:
        raise ValueError("real-context epoch retained no counterfactual states")
    expected = 4 * sum(entry["augmentation"]["samples"] for entry in entries)
    if seen != expected:
        raise ValueError("real-context epoch did not expose each source state four times")
    return dict(epoch=epoch, samples=seen, steps=steps, soft_ce=total_ce / seen,
        max_unclipped_gradient_norm=max_norm, first_batch_gradient_l1=first_gradients,
        episode_order_sha=semantic_digest([entry["source_id"] for entry in order]),
        exposure_per_source_state=4)


def _save(path, model, optimizer, identity, state):
    return save_checkpoint(path, model, optimizer, identity=identity,
        optimizer_steps=int(state["steps"]), run_state=state)


def _load(path, model, optimizer, identity, device):
    return load_checkpoint(path, model, optimizer, expected_identity=identity, device=device)


def _read_initial_model_checkpoint(path, model, expected_model_sha):
    path = Path(path)
    envelope = torch.load(path, weights_only=True, map_location="cpu")
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if (envelope.get("schema") != CHECKPOINT_SCHEMA or not isinstance(payload, dict)
            or semantic_digest(payload) != envelope.get("payload_sha")
            or payload.get("schema") != CHECKPOINT_SCHEMA
            or payload.get("architecture") != model.architecture
            or payload.get("architecture_sha") != model.architecture_sha
            or semantic_digest(payload.get("model")) != payload.get("model_sha")
            or payload.get("model_sha") != expected_model_sha
            or payload.get("optimizer_steps") != 0
            or (payload.get("run_state") or {}).get("steps") != 0
            or semantic_digest(payload.get("run_state")) != payload.get("run_state_sha")):
        raise ValueError("initial tower model checkpoint is not a verified zero-update seed")
    model.load_state_dict(payload["model"], strict=True)
    return payload["model"], file_sha(path)


def _restore_parent(path, expected_sha, model, optimizer, *, device, expected_identity):
    if file_sha(path) != expected_sha:
        raise ValueError("synthetic parent checkpoint checksum differs")
    result = _load(path, model, optimizer, expected_identity, device)
    payload = torch.load(path, weights_only=True, map_location="cpu")["payload"]
    if (payload["model_sha"] != semantic_digest(model.state_dict())
            or payload["optimizer_sha"] != semantic_digest(optimizer.state_dict())):
        raise ValueError("synthetic model/AdamW parent restore differs")
    return result


def _constant_metrics(records, score):
    return score_metrics(records, [float(score)] * len(records), replicates=0)


def _metrics_for_examples(model, examples, device):
    records, predictions = _score_predictions(model, examples, device=device)
    return score_metrics(records, predictions, replicates=0)


def _run_stage(stage_dir, model, optimizer, identity, *, train_epoch, validation,
        max_epochs=MAX_EPOCHS, starting_steps=0):
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    result_path = stage_dir / "result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text())
        if result.get("identity") != identity or file_sha(stage_dir / "best.pt") != result.get("checkpoint_sha"):
            raise ValueError("completed tower stage identity or checkpoint differs")
        _load(stage_dir / "best.pt", model, optimizer, identity, next(model.parameters()).device)
        return result
    device = next(model.parameters()).device
    best_path, latest_path = stage_dir / "best.pt", stage_dir / "latest.pt"
    if latest_path.exists():
        loaded = _load(latest_path, model, optimizer, identity, device)
        state = loaded["run_state"]
    else:
        baseline = validation(model)
        state = dict(epoch=0, steps=starting_steps, best_epoch=0, best_mse=baseline["mse"],
            best_metrics=baseline, baseline_metrics=baseline, epochs=[])
        _save(best_path, model, optimizer, identity, state)
        _save(latest_path, model, optimizer, identity, state)
    while state["epoch"] < max_epochs:
        epoch = state["epoch"] + 1
        _atomic_json(stage_dir / "status.json", dict(status="running", stage=identity["stage"],
            epoch=epoch, maximum=max_epochs, optimizer_steps=state["steps"]))
        started = time.perf_counter()
        update = train_epoch(epoch)
        metrics = validation(model)
        state["epoch"] = epoch
        state["steps"] += update["steps"]
        improved = metrics["mse"] < state["best_mse"]
        if improved:
            state["best_epoch"] = epoch
            state["best_mse"] = metrics["mse"]
            state["best_metrics"] = metrics
        record = dict(**update, validation=metrics, improved=improved,
            seconds=time.perf_counter() - started)
        state["epochs"].append(record)
        _atomic_json(stage_dir / f"epoch-{epoch:04d}.json", record)
        if improved:
            _save(best_path, model, optimizer, identity, state)
        _save(latest_path, model, optimizer, identity, state)
        _atomic_json(stage_dir / "status.json", dict(status="epoch_complete", stage=identity["stage"],
            epoch=epoch, maximum=max_epochs, validation_mse=metrics["mse"],
            best_epoch=state["best_epoch"], best_mse=state["best_mse"], seconds=record["seconds"]))
    restored = _restore_parent(best_path, file_sha(best_path), model, optimizer, device=device,
        expected_identity=identity)
    reference = initialize_fresh_model(SEED, device=device)
    reference_optimizer = _make_optimizer(reference)
    _load(best_path, reference, reference_optimizer, identity, device)
    roundtrip = semantic_digest((model.state_dict(), optimizer.state_dict())) == semantic_digest(
        (reference.state_dict(), reference_optimizer.state_dict()))
    if not roundtrip:
        raise ValueError("tower stage best model and AdamW checkpoint roundtrip differs")
    report = dict(schema=RUN_SCHEMA, identity=identity, status="complete", epochs=state["epoch"],
        best_epoch=state["best_epoch"], best_metrics=state["best_metrics"],
        baseline_metrics=state["baseline_metrics"], history=state["epochs"],
        optimizer_steps=restored["run_state"]["steps"], attempted_steps=state["steps"],
        checkpoint_sha=file_sha(best_path),
        model_sha=semantic_digest(model.state_dict()), optimizer_sha=semantic_digest(optimizer.state_dict()),
        checkpoint_roundtrip=roundtrip)
    _write_json(result_path, report)
    _atomic_json(stage_dir / "status.json", dict(status="complete", stage=identity["stage"],
        best_epoch=state["best_epoch"], best_mse=state["best_mse"], checkpoint_sha=report["checkpoint_sha"]))
    return report


def _activate_run_identity(run_dir, identity):
    """Archive a source-only retry after a recorded pre-checkpoint failure."""
    run_dir = Path(run_dir)
    config_path = run_dir / "run_config.json"
    if not config_path.exists():
        if any(run_dir.glob("stage-*/*.pt")):
            raise ValueError("run has stage checkpoints but no matching immutable run identity")
        _write_json(config_path, identity)
        return

    previous = json.loads(config_path.read_text())
    if previous == identity:
        return
    old_identity, new_identity = dict(previous), dict(identity)
    old_source_sha = old_identity.pop("source_sha", None)
    new_source_sha = new_identity.pop("source_sha", None)
    status_path = run_dir / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    if (old_identity != new_identity or old_source_sha == new_source_sha
            or status.get("status") != "paused_failure"
            or any(run_dir.glob("stage-*/*.pt"))):
        raise ValueError("existing counterfactual run has a different immutable identity")

    attempts = run_dir / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    index = 1
    while (attempts / f"attempt-{index:03d}").exists():
        index += 1
    archive = attempts / f"attempt-{index:03d}"
    archive.mkdir()
    for name in ("run_config.json", "status.json", "backend-probe.json",
            "backend-probe-summary.json"):
        source = run_dir / name
        if source.exists():
            shutil.copy2(source, archive / name)
    _write_json(config_path, identity)


def _synthetic_templates(manifest, data_dir, entries, model_sha):
    by_id = (entries if isinstance(entries, dict)
        else {entry["source_id"]: entry for entry in entries})
    templates = {"training": [], "validation": []}
    for split in ("training", "validation"):
        for descriptor in manifest["synthetic_templates"][split]:
            entry = by_id[descriptor["source_id"]]
            states, _ = load_episode_states(entry, model_sha=model_sha,
                augmentation=_load_augmentation(data_dir, entry))
            state = next((item for item in states
                if item.information.owner == descriptor["owner"]
                and item.decision_index == descriptor["decision_index"]), None)
            if (state is None or state.information.sha256 != descriptor["fair_sha256"]
                    or not _is_minimal_scene(state.information, state.tower_target)):
                raise ValueError("synthetic clean-scene template identity differs")
            _hp_bounds(state.information, state.tower_target, synthetic=True)
            templates[split].append(state)
        identities = [(row.source_id, row.information.owner, row.decision_index)
            for row in templates[split]]
        if len(set(identities)) != len(identities):
            raise ValueError("synthetic split repeats a source state identity")
    if ({row.source_id for row in templates["training"]}
            & {row.source_id for row in templates["validation"]}):
        raise ValueError("synthetic templates leak a complete source game across splits")
    return templates


def _synthetic_examples(templates):
    examples = []
    for split, scenes in templates.items():
        if len(scenes) != SYNTH_TEMPLATES_PER_SPLIT:
            raise ValueError("synthetic phase requires eight episode-disjoint templates per split")
        for score_index in range(SYNTH_SCORE_BINS):
            for combination_index, factors in enumerate(SYNTH_COMBINATIONS):
                template_index = (score_index * len(SYNTH_COMBINATIONS) + combination_index) % len(scenes)
                state = scenes[template_index]
                bounds = _hp_bounds(state.information, state.tower_target, synthetic=True)[2]
                low, high = _score_range(bounds, state.information.owner)
                score = low + (high - low) * ((score_index + 0.5) / SYNTH_SCORE_BINS)
                hp, _actual = hp_values_for_score(state.information, state.tower_target, score,
                    factors, synthetic=True)
                target = _target_from_hp(state.tower_target, state.information.owner, hp)
                examples.append(Example(state.information, target, tuple(hp), state.tower_target,
                    state.source_id,
                    f"synthetic:{split}:{score_index}:{combination_index}"))
    return examples


def _metric_baselines(examples):
    records = [dict(game_id=example.game_id, score=example.tower_target["score"], state_id=example.state_id)
        for example in examples]
    mean = sum(record["score"] for record in records) / len(records)
    return dict(mean=mean, zero=_constant_metrics(records, 0.),
        training_mean=_constant_metrics(records, mean)), records


def _example_from_state(state, variant_index):
    if variant_index == 0:
        values = [next(row["hitpoints"] for row in state.tower_target["towers"]
            if (row["owner"], row["tower_kind"]) == key) for key in TOWER_KEYS]
        target = _target_from_hp(state.tower_target, state.information.owner, values)
        return Example(state.information, target, None, state.tower_target, state.source_id,
            f"{state.source_id}:{state.information.owner}:{state.decision_index}:0")
    variant = state.variants[variant_index - 1]
    return Example(state.information, variant, variant["tower_hp"], state.tower_target,
        state.source_id,
        f"{state.source_id}:{state.information.owner}:{state.decision_index}:{variant_index}")


def _real_examples(states):
    for state in states:
        for variant_index in range(4):
            yield _example_from_state(state, variant_index)


def _evaluate_real_split(model, entries, data_dir, model_sha, *, device, replicates=0):
    records, predictions = [], []
    for entry in entries:
        augmentation = (_load_augmentation(data_dir, entry)
            if entry.get("augmentation") is not None else None)
        states, _metadata = load_episode_states(entry, model_sha=model_sha, augmentation=augmentation)
        game_records, game_predictions = _score_predictions(model, list(_real_examples(states)), device=device)
        records.extend(game_records)
        predictions.extend(game_predictions)
    return score_metrics(records, predictions, replicates=replicates), records, predictions


def _real_target_records(entries, data_dir):
    records = []
    for entry in entries:
        augmentation = _load_augmentation(data_dir, entry)
        for row in augmentation["records"]:
            state_id = f"{entry['source_id']}:{row['owner']}:{row['decision_index']}"
            records.append(dict(game_id=entry["source_id"], score=row["original_score"],
                state_id=f"{state_id}:0"))
            records.extend(dict(game_id=entry["source_id"], score=item["score"],
                state_id=f"{state_id}:{index}")
                for index, item in enumerate(row["variants"], start=1))
    return records


def _backend_probe(examples, run_dir, initial_model_state, initial_model_source_sha):
    if not isinstance(examples, (list, tuple)):
        examples = [examples]
    if not examples:
        raise ValueError("backend probe requires at least one complete FAIR example")
    path = Path(run_dir) / "backend-probe.json"
    probe_identity = dict(seed=SEED,
        fair_sha256s=[example.information.sha256 for example in examples],
        example_digest=semantic_digest([(example.state_id, example.hp_by_key,
            example.tower_target["score"]) for example in examples]),
        model_sha=semantic_digest(initial_model_state),
        initial_model_source_sha=initial_model_source_sha,
        optimizer=dict(lr=LR, weight_decay=WEIGHT_DECAY), examples=len(examples), steps=3,
        backend_candidates=["cpu", "mps", "cuda"])
    if path.exists():
        record = json.loads(path.read_text())
        if record.get("identity") != probe_identity:
            raise ValueError("backend probe identity differs")
        return record["selected_device"], record
    results = {}
    initial_logits = {}
    for device in ("cpu", "mps", "cuda"):
        if device == "mps" and not torch.backends.mps.is_available():
            results[device] = dict(status="unavailable")
            continue
        if device == "cuda" and not torch.cuda.is_available():
            results[device] = dict(status="unavailable")
            continue
        model = initialize_fresh_model(SEED, device=device)
        model.load_state_dict(initial_model_state, strict=True)
        optimizer = _make_optimizer(model)
        policy_sha = policy_head_sha(model)
        trainable_before = semantic_digest([parameter.detach()
            for parameter in model.parameters() if parameter.requires_grad])
        timings = []
        first = None
        try:
            for _ in range(3):
                optimizer.zero_grad(set_to_none=True)
                start = time.perf_counter()
                frame, hands, target = _batch_tensors(examples, device)
                logits = model.value_batch(frame, hands)
                loss = soft_loss(logits, target)
                loss.backward()
                _gradient_summary(model)
                torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], GRADIENT_CLIP)
                optimizer.step()
                if device == "mps":
                    torch.mps.synchronize()
                elif device == "cuda":
                    torch.cuda.synchronize()
                timings.append(time.perf_counter() - start)
                if first is None:
                    first = logits.detach().cpu()
            initial_logits[device] = first
            trainable_after = semantic_digest([parameter.detach()
                for parameter in model.parameters() if parameter.requires_grad])
            if trainable_after == trainable_before:
                raise ValueError("backend probe performed no parameter update")
            if policy_head_sha(model) != policy_sha:
                raise ValueError("backend probe changed the frozen policy head")
            results[device] = dict(status="passed", step_seconds=timings,
                median_seconds=statistics.median(timings), model_sha=semantic_digest(model.state_dict()))
        except Exception as error:
            results[device] = dict(status="failed", error=type(error).__name__, reason=str(error))
    if results.get("cpu", {}).get("status") != "passed":
        raise RuntimeError("CPU correctness and update probe failed")
    numeric_diffs = {}
    for device in ("mps", "cuda"):
        if results.get(device, {}).get("status") != "passed":
            continue
        max_diff = float((initial_logits["cpu"] - initial_logits[device]).abs().max())
        numeric_diffs[device] = max_diff
        if max_diff > 1e-4:
            results[device]["status"] = "failed_numeric"
            results[device]["reason"] = f"initial logits differ from CPU by {max_diff}"
    selected = _select_backend(results)
    record = dict(identity=probe_identity, torch_version=str(torch.__version__), backends=results,
        initial_logit_max_abs_diff=numeric_diffs, selected_device=selected)
    _write_json(path, record)
    return selected, record


def _select_backend(results, *, require_cuda=False):
    eligible = [device for device in ("cpu", "mps", "cuda")
        if results.get(device, {}).get("status") == "passed"]
    if require_cuda and "cuda" not in eligible:
        raise RuntimeError("CUDA was required but failed its correctness and numeric probes")
    if not eligible:
        raise RuntimeError("no backend passed the update and numeric probes")
    if "cuda" in eligible:
        return "cuda"
    return min(eligible, key=lambda device: results[device]["median_seconds"])


def _training_constant_baselines(entries, data_dir):
    records = _real_target_records(entries, data_dir)
    if not records:
        raise ValueError("real-context training split contains no target records")
    mean = sum(row["score"] for row in records) / len(records)
    return dict(mean=mean, zero=_constant_metrics(records, 0.),
        training_mean=_constant_metrics(records, mean)), records


def _stage_identity(run_identity, stage, starting_model_sha, starting_optimizer_sha=None,
        parent_checkpoint_sha=None):
    return dict(**run_identity, stage=stage, starting_model_sha=starting_model_sha,
        starting_optimizer_sha=starting_optimizer_sha,
        parent_checkpoint_sha=parent_checkpoint_sha)


def run(bootstrap_run, refresh_root, data_dir, run_dir, run_id, initial_model_checkpoint):
    data_dir, run_dir = Path(data_dir), Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.manual_seed(SEED)
    random.seed(SEED)
    cuda_workspace = os.environ.get("CUBLAS_WORKSPACE_CONFIG")
    if torch.cuda.is_available() and cuda_workspace not in (":4096:8", ":16:8"):
        raise RuntimeError("CUDA tower training requires deterministic CuBLAS workspace configuration")

    manifest = prepare_dataset(bootstrap_run, refresh_root, data_dir, run_id)
    dataset_sha = file_sha(data_dir / "dataset.json")
    source_entries = {item["source_id"]: item for item in manifest["sources"]}
    training_entries = [item for item in manifest["sources"] if item["split"] == "training"]
    validation_entries = [item for item in manifest["sources"] if item["split"] == "validation"]
    heldout_entries = [item for item in manifest["sources"] if item["split"] == "heldout"]
    model_sha = manifest["identity"]["initial_model_sha"]
    templates = _synthetic_templates(manifest, data_dir, source_entries, model_sha)
    synthetic = {name: _synthetic_examples({name: templates[name]})
        for name in ("training", "validation")}
    if not synthetic["training"] or not synthetic["validation"]:
        raise ValueError("synthetic train/validation set is empty")

    initial_model = initialize_fresh_model(SEED, device="cpu")
    initial_model_state, initial_model_source_sha = _read_initial_model_checkpoint(
        initial_model_checkpoint, initial_model, model_sha)
    device, backend_probe = _backend_probe(synthetic["training"][:MICROBATCH], run_dir,
        initial_model_state, initial_model_source_sha)
    if not torch.cuda.is_available() or device != "cuda":
        raise RuntimeError("tower counterfactual training requires a passing CUDA backend")
    torch.manual_seed(SEED)
    random.seed(SEED)
    model = initialize_fresh_model(SEED, device=device)
    model.load_state_dict(initial_model_state, strict=True)
    if semantic_digest(model.state_dict()) != model_sha:
        raise ValueError("loaded model does not match the frozen random initialization identity")
    optimizer = _make_optimizer(model)
    policy_sha = policy_head_sha(model)
    initial_identity = dict(protocol=RUN_SCHEMA, run_id=run_id, dataset_sha=dataset_sha,
        source_sha=source_sha(), initial_model_sha=model_sha, seed=SEED,
        initial_model_source_sha=initial_model_source_sha,
        device=device, torch_version=str(torch.__version__), batch=BATCH,
        cublas_workspace_config=cuda_workspace if device == "cuda" else None,
        microbatch=MICROBATCH, lr=LR, weight_decay=WEIGHT_DECAY,
        maximum_epochs=MAX_EPOCHS, policy_head_sha=policy_sha,
        original_holdout_evaluated=False)
    _activate_run_identity(run_dir, initial_identity)
    _write_json(run_dir / "backend-probe-summary.json", backend_probe)
    _write_json(run_dir / "status.json", dict(status="running", stage="synthetic_data",
        dataset_sha=dataset_sha, split_sizes=manifest["identity"]["split_sizes"], device=device))

    synthetic_train = synthetic["training"]
    synthetic_validation = synthetic["validation"]
    synthetic_baselines, _ = _metric_baselines(synthetic_train)
    synthetic_validation_records = [dict(game_id=example.game_id,
        score=example.tower_target["score"], state_id=example.state_id)
        for example in synthetic_validation]
    synthetic_validation_baselines = dict(
        zero=_constant_metrics(synthetic_validation_records, 0.),
        training_mean=_constant_metrics(synthetic_validation_records, synthetic_baselines["mean"]),
        training_mean_value=synthetic_baselines["mean"])
    synthetic_stage_identity = _stage_identity(initial_identity, "synthetic",
        semantic_digest(model.state_dict()), semantic_digest(optimizer.state_dict()))
    synthetic_stage = _run_stage(run_dir / "stage-synthetic", model, optimizer,
        synthetic_stage_identity,
        train_epoch=lambda epoch: train_examples(model, optimizer, synthetic_train,
            epoch=epoch, device=device, seed_key=0x53594E54),
        validation=lambda current_model: _metrics_for_examples(current_model,
            synthetic_validation, device),
        starting_steps=0)
    if policy_head_sha(model) != policy_sha:
        raise ValueError("synthetic value training changed the frozen policy head")

    parent_checkpoint = run_dir / "stage-synthetic" / "best.pt"
    parent_checkpoint_sha = file_sha(parent_checkpoint)
    parent_model_sha = semantic_digest(model.state_dict())
    parent_optimizer_sha = semantic_digest(optimizer.state_dict())
    real_context_identity = _stage_identity(initial_identity, "real_context",
        parent_model_sha, parent_optimizer_sha, parent_checkpoint_sha)
    _write_json(run_dir / "status.json", dict(status="running", stage="real_context",
        parent_checkpoint_sha=parent_checkpoint_sha, parent_model_sha=parent_model_sha))
    real_baselines, training_target_records = _training_constant_baselines(training_entries, data_dir)
    real_validation = lambda current_model: _evaluate_real_split(current_model, validation_entries, data_dir,
        model_sha, device=device, replicates=0)[0]
    real_stage = _run_stage(run_dir / "stage-real-context", model, optimizer,
        real_context_identity,
        train_epoch=lambda epoch: train_real_epoch(model, optimizer, training_entries,
            data_dir, model_sha, epoch=epoch, device=device),
        validation=real_validation, starting_steps=synthetic_stage["optimizer_steps"])
    if policy_head_sha(model) != policy_sha:
        raise ValueError("real-context value training changed the frozen policy head")

    final_checkpoint = run_dir / "stage-real-context" / "best.pt"
    final_checkpoint_sha = file_sha(final_checkpoint)
    heldout_path = run_dir / "heldout-evaluation.json"
    heldout_reused = heldout_path.exists()
    if heldout_path.exists():
        heldout = json.loads(heldout_path.read_text())
        if (heldout.get("run_id") != run_id or heldout.get("dataset_sha") != dataset_sha
                or heldout.get("checkpoint_sha") != final_checkpoint_sha):
            raise ValueError("cached held-out inference belongs to another run or checkpoint")
        heldout_payload = {key: heldout[key] for key in
            ("run_id", "dataset_sha", "checkpoint_sha", "records", "predictions", "metrics")}
        if heldout.get("payload_sha") != semantic_digest(heldout_payload):
            raise ValueError("cached held-out inference payload checksum differs")
        records, predictions = heldout["records"], heldout["predictions"]
        final_metrics = heldout["metrics"]
    else:
        _write_json(run_dir / "status.json", dict(status="running", stage="heldout_once",
            checkpoint_sha=final_checkpoint_sha, heldout_games=len(heldout_entries)))
        final_model = initialize_fresh_model(SEED, device=device)
        final_optimizer = _make_optimizer(final_model)
        _load(final_checkpoint, final_model, final_optimizer, real_context_identity, device)
        records, predictions = [], []
        for entry in heldout_entries:
            states, _ = load_episode_states(entry, model_sha=model_sha, augmentation=None)
            game_records, game_predictions = _score_predictions(final_model,
                list(_real_examples(states)), device=device)
            records.extend(game_records)
            predictions.extend(game_predictions)
        final_metrics = score_metrics(records, predictions, replicates=BOOTSTRAP_REPLICATES)
        heldout = dict(run_id=run_id, dataset_sha=dataset_sha,
            checkpoint_sha=final_checkpoint_sha, records=records,
            predictions=predictions, metrics=final_metrics)
        heldout["payload_sha"] = semantic_digest(heldout)
        _write_json(heldout_path, heldout)

    if not training_target_records:
        raise ValueError("real-context training baseline is empty")
    training_mean = sum(row["score"] for row in training_target_records) / len(training_target_records)
    zero_baseline = _constant_metrics(records, 0.)
    mean_baseline = _constant_metrics(records, training_mean)
    best_constant = min(zero_baseline["mse"], mean_baseline["mse"])
    acceptance = dict(passed=bool(final_metrics["mse"] <= 0.5 * best_constant
            and final_metrics["mae"] <= 0.05
            and final_metrics["direction_accuracy"] is not None
            and final_metrics["direction_accuracy"] >= 0.8
            and final_metrics["advantage_games"] >= 4
            and final_metrics["advantage_states"] >= 100),
        mse_half_best_constant=final_metrics["mse"] <= 0.5 * best_constant,
        mae_limit=final_metrics["mae"] <= 0.05,
        direction_limit=final_metrics["direction_accuracy"] is not None
            and final_metrics["direction_accuracy"] >= 0.8,
        advantage_coverage=final_metrics["advantage_games"] >= 4
            and final_metrics["advantage_states"] >= 100)
    report = dict(schema=RUN_SCHEMA, identity=initial_identity, status="complete",
        dataset=dict(path=str(data_dir), sha256=dataset_sha, totals=manifest["totals"],
            source_games=288, split_sizes=manifest["identity"]["split_sizes"],
            heldout_from_existing_games=True, new_games=0),
        backend_probe=backend_probe, selected_device=device,
        policy_head_sha_initial=policy_sha,
        synthetic_stage=synthetic_stage, real_context_stage=real_stage,
        synthetic_validation=synthetic_stage["best_metrics"],
        synthetic_validation_baselines=synthetic_validation_baselines,
        real_context_validation=real_stage["best_metrics"],
        heldout=dict(metrics=final_metrics, constants=dict(zero=zero_baseline,
            training_mean=mean_baseline, training_mean_value=training_mean),
            best_constant_mse=best_constant, acceptance=acceptance,
            reused_after_checkpointed_inference=heldout_reused,
            inference_file_sha256=file_sha(heldout_path)),
        final_checkpoint=dict(path=str(final_checkpoint), sha256=final_checkpoint_sha,
            model_sha=real_stage["model_sha"], optimizer_sha=real_stage["optimizer_sha"]),
        split_note="Held-out episodes are from prior games and were excluded by game identity during both training stages; this is not newly collected independent data.",
        search_roots=0, native_games=0, learned_win_probability_claim=False)
    _write_json(run_dir / "result.json", report)
    _write_json(run_dir / "status.json", dict(status="complete",
        acceptance=acceptance, checkpoint_sha=final_checkpoint_sha))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-run", type=Path, required=True)
    parser.add_argument("--refresh-root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--initial-model-checkpoint", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (run_dir / "trainer.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            run(args.bootstrap_run, args.refresh_root, args.data_dir, run_dir, args.run_id,
                args.initial_model_checkpoint)
        except BaseException as error:
            _write_json(run_dir / "status.json", dict(status="paused_failure",
                error=type(error).__name__, reason=str(error), traceback=traceback.format_exc()))
            raise


if __name__ == "__main__":
    main()
