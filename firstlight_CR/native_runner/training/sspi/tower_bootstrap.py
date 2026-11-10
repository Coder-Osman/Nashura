"""Fixed tower-HP teacher and head-only warmup; scores are not win probabilities."""

from __future__ import annotations

import copy
import math
from pathlib import Path
import random

import torch

from ..fair_search.contracts import semantic_digest
from .learning import load_checkpoint, save_checkpoint
from .model import VALUE_ORDER
from .value_learning import configure_value_training, policy_function_sha

TARGET_SCHEMA = "sspi-public-tower-hp-target.v1"
CACHE_SCHEMA = "sspi-tower-head-cache.v2"
RUN_SCHEMA = "sspi-tower-head-bootstrap.v2"
FORMULA = "sum-hp-difference-over-sum.v1"
SEED = 20261103
BATCH = 256
MAX_EPOCHS = 20
PATIENCE = 3
MIN_IMPROVEMENT = 1e-6


def tower_target(towers, owner):
    """Six explicit public towers are required; masked padding is never evidence."""
    if type(owner) is not int or owner not in (0, 1):
        raise ValueError("invalid tower perspective")
    expected = {(side, kind) for side in (0, 1)
        for kind in ("king", "princess_left", "princess_right")}
    seen, total = set(), [0., 0.]
    for row in towers:
        if row.get("mask") is False:
            continue
        key = (row.get("owner"), row.get("tower_kind"))
        if key not in expected or key in seen:
            raise ValueError("invalid or duplicate public tower identity")
        seen.add(key)
        hp, maximum = row.get("hitpoints"), row.get("max_hitpoints")
        if (type(hp) not in (int, float) or type(maximum) not in (int, float)
                or not math.isfinite(hp) or not math.isfinite(maximum)
                or maximum <= 0 or not 0 <= hp <= maximum
                or type(row.get("active")) is not bool
                or (hp == 0 and row["active"])):
            raise ValueError("missing or invalid public tower HP/destruction evidence")
        total[key[0]] += hp
    if seen != expected or sum(total) <= 0:
        raise ValueError("missing public towers or zero tower HP denominator")
    own, enemy = total[owner], total[1 - owner]
    score = (own - enemy) / (own + enemy)
    return dict(formula=FORMULA, self_hp=own, enemy_hp=enemy, score=score,
        soft_target=[(1. + score) / 2., (1. - score) / 2.], value_order=list(VALUE_ORDER))


def capture_tower_target(observation, information):
    # ObservationV1 retains destroyed fixed tower identities with HP=0, active=False.
    towers = [dict(owner=t.owner, tower_kind=t.tower_kind, hitpoints=t.hitpoints,
        max_hitpoints=t.max_hitpoints, active=t.active) for t in observation.towers]
    record = dict(schema=TARGET_SCHEMA, owner=information.owner, tick=information.tick,
        fair_sha256=information.sha256, towers=towers)
    try:
        if observation.owner != information.owner or observation.tick != information.tick:
            raise ValueError("tower view differs from the frozen decision")
        record.update(status="valid", **tower_target(towers, information.owner))
    except ValueError as error:
        record.update(status="quarantined", reason=str(error))
    return record


def validate_target(record, raw):
    if (record.get("schema") != TARGET_SCHEMA or record.get("owner") != raw["owner"]
            or record.get("tick") != raw["tick"]
            or record.get("fair_sha256") != raw["fair_sha256"]):
        raise ValueError("heuristic target identity differs from FAIR input")
    try:
        expected = tower_target(record["towers"], record["owner"])
    except ValueError:
        if record.get("status") == "quarantined":
            return False
        raise
    if record.get("status") != "valid" or any(record.get(k) != v for k, v in expected.items()):
        raise ValueError("heuristic formula or target differs")
    return True


def create_optimizer(model):
    parameters = configure_value_training(model, head_only=True)
    model.eval()  # Freeze all shared running statistics as well as weights.
    expected_parameters = tuple(model.value_head.parameters())
    if ({id(p) for p in parameters} != {id(p) for p in expected_parameters}
            or any(p.requires_grad for n, p in model.named_parameters() if not n.startswith("value_head."))):
        raise ValueError("tower warmup optimizer must contain exactly the current value head")
    if model.architecture["value_order"] != ["WIN", "LOSS"]:
        raise ValueError("tower warmup requires explicit WIN/LOSS output order")
    return torch.optim.AdamW(parameters, lr=1e-3, weight_decay=1e-4,
        betas=(.9, .999), eps=1e-8)


def soft_loss(logits, targets):
    if (logits.ndim != 2 or logits.shape != targets.shape or logits.shape[1] != 2
            or not len(logits) or not torch.isfinite(logits).all()
            or not torch.isfinite(targets).all() or bool((targets < 0).any())
            or not torch.allclose(targets.sum(-1), torch.ones_like(targets[:, 0]), atol=1e-6, rtol=0)):
        raise ValueError("invalid soft WIN/LOSS batch")
    return -(targets * logits.log_softmax(-1)).sum(-1).mean()


def policy_signature(model, information):
    """Fingerprint every reachable conditional, including legal cell/delay distributions."""
    from .protocol import ActionMode
    with torch.no_grad():
        policy = model(information, [information.legality])[0][0]
        result = {"mode": policy.conditionals()[information.legality.modes].tolist()}
        if bool(information.legality.modes[ActionMode.PLAY]):
            prefix = (ActionMode.PLAY,)
            result["cards"] = policy.conditionals(prefix)[information.legality.mask(prefix)].tolist()
            for c in information.legality.plays.any(-1).any(-1).nonzero().flatten().tolist():
                prefix = (ActionMode.PLAY, c)
                result[f"cells-{c}"] = policy.conditionals(prefix)[information.legality.mask(prefix)].tolist()
                for cell in information.legality.plays[c].any(-1).nonzero().flatten().tolist():
                    prefix = (ActionMode.PLAY, c, cell)
                    result[f"delays-{c}-{cell}"] = policy.conditionals(prefix)[information.legality.mask(prefix)].tolist()
    return semantic_digest(result)


def cache_rows(model, rows, raw_rows):
    """One-time encoding; caches contain normal tensors usable by head autograd."""
    model.eval()
    before = policy_function_sha(model)
    vectors, records, excluded = [], [], []
    max_error = 0.
    if len(rows) != len(raw_rows):
        raise ValueError("heuristic rows differ from complete value episode")
    for index, (row, raw) in enumerate(zip(rows, raw_rows)):
        target = raw.get("tower_hp_target") or {}
        if not validate_target(target, raw):
            excluded.append(dict(owner=raw["owner"], tick=raw["tick"],
                decision_index=raw["decision_index"], reason=target["reason"]))
            continue
        with torch.no_grad():
            shared, encoded, _ = model.encode(row.information)
            value_inputs = model.value_head_inputs(shared, encoded)
            if value_inputs.shape != (1, 1024) or not bool(torch.isfinite(value_inputs).all()):
                raise ValueError("invalid frozen shared and FAIR value representation")
            if index in (0, len(rows) // 2, len(rows) - 1):
                cached = model.value_head(value_inputs)
                full = model(row.information, [row.information.legality])[1]
                max_error = max(max_error, float((full - cached).abs().max()))
                torch.testing.assert_close(full, cached, atol=1e-6, rtol=0)
        vectors.append(value_inputs.detach().cpu().clone())
        records.append(dict(game_id=row.game_id,
            decision_index=raw["decision_index"], value_gate_seed=raw["value_gate_seed"],
            action=raw["action"], executed_action=raw["executed_action"], **target))
    if policy_function_sha(model) != before:
        raise ValueError("shared weights or buffers changed during cache encoding")
    return dict(schema=CACHE_SCHEMA, frozen_function_sha=before,
        value_inputs=torch.cat(vectors) if vectors else torch.empty((0, 1024)), records=records,
        excluded=excluded, cache_forward_max_abs_error=max_error)


def cached_scores(model, cache):
    model.eval()
    with torch.no_grad():
        chunks = [model.value_head(part).softmax(-1) for part in cache["value_inputs"].split(BATCH)]
    return torch.cat([p[:, 0] - p[:, 1] for p in chunks]).tolist() if chunks else []


def _mean(values):
    return sum(values) / len(values) if values else None


def _correlation(x, y):
    # A nonzero constant can acquire a tiny fake variance through rounded means.
    if len(x) < 2 or min(x) == max(x) or min(y) == max(y):
        return None
    mean_x, mean_y = _mean(x), _mean(y)
    dx, dy = [v - mean_x for v in x], [v - mean_y for v in y]
    denominator = math.sqrt(sum(v*v for v in dx) * sum(v*v for v in dy))
    return sum(a*b for a, b in zip(dx, dy)) / denominator if denominator > 0 else None


def score_metrics(records, predictions, *, seed=SEED, replicates=2000):
    if len(records) != len(predictions) or any(not math.isfinite(p) or abs(p) > 1.000001 for p in predictions):
        raise ValueError("invalid heuristic predictions")
    grouped = {}
    for row, prediction in zip(records, predictions):
        grouped.setdefault(row["game_id"], []).append((row["score"], prediction))
    games = []
    for game, pairs in sorted(grouped.items()):
        actual, predicted = zip(*pairs)
        advantage = [(a, p) for a, p in pairs if abs(a) >= .05]
        games.append(dict(game_id=game, samples=len(pairs),
            mse=_mean([(a-p)**2 for a, p in pairs]), mae=_mean([abs(a-p) for a, p in pairs]),
            correlation=_correlation(actual, predicted), advantage_states=len(advantage),
            direction_accuracy=_mean([float(a*p > 0) for a, p in advantage])))
    keys = ("mse", "mae", "correlation", "direction_accuracy")
    def aggregate(selected, key):
        return _mean([g[key] for g in selected if g[key] is not None])
    rng = random.Random(seed)
    intervals = {key: [] for key in keys}
    for _ in range(replicates if games else 0):
        selected = [games[rng.randrange(len(games))] for _ in games]
        for key in keys:
            value = aggregate(selected, key)
            if value is not None:
                intervals[key].append(value)
    def interval(values):
        values.sort()
        return ([values[int(.025*(len(values)-1))], values[int(.975*(len(values)-1))]]
            if values else None)
    return dict(status="defined" if games else "undefined", games=len(games), samples=len(records),
        **{key: aggregate(games, key) for key in keys},
        advantage_games=sum(g["advantage_states"] > 0 for g in games),
        advantage_states=sum(g["advantage_states"] for g in games),
        ci95={key: interval(values) for key, values in intervals.items()}, per_game=games,
        aggregation="equal-game-means", bootstrap="whole-game-resampling", bootstrap_seed=seed,
        bootstrap_replicates=replicates, correlation_defined_games=sum(g["correlation"] is not None for g in games))


def train_head(model, optimizer, train, validation, *, checkpoint_path, identity, epoch_callback=None):
    """Resume at committed epoch boundaries, with exact best head AND AdamW state."""
    frozen = policy_function_sha(model)
    for cache in (train, validation):
        if (cache["schema"] != CACHE_SCHEMA or cache["frozen_function_sha"] != frozen
                or len(cache["value_inputs"]) != len(cache["records"])
                or cache["value_inputs"].shape[1:] != (1024,)):
            raise ValueError("cache identity or row count differs")
    if {r["game_id"] for r in train["records"]} & {r["game_id"] for r in validation["records"]}:
        raise ValueError("training/validation episode overlap")
    if not train["records"] or not validation["records"]:
        return dict(status="skipped", reason="empty effective training or validation data", attempted_steps=0)
    parameters = tuple(model.value_head.parameters())
    if ({id(p) for g in optimizer.param_groups for p in g["params"]} != {id(p) for p in parameters}
            or any(p.requires_grad for n, p in model.named_parameters() if not n.startswith("value_head."))):
        raise ValueError("tower optimizer scope differs")
    data_sha = semantic_digest(dict(train=train, validation=validation))
    def mse():
        return score_metrics(validation["records"], cached_scores(model, validation), replicates=0)["mse"]
    path = Path(checkpoint_path)
    if path.exists():
        state = load_checkpoint(path, model, optimizer, expected_identity=identity)["run_state"]
        if state["data_sha"] != data_sha or state["frozen_sha"] != frozen:
            raise ValueError("warmup resume data or frozen identity differs")
    else:
        initial_mse = mse()
        state = dict(data_sha=data_sha, frozen_sha=frozen, epoch=0, steps=0,
            initial_mse=initial_mse, best_mse=initial_mse, best_epoch=0, best_steps=0,
            best_head=copy.deepcopy(model.value_head.state_dict()),
            best_optimizer=copy.deepcopy(optimizer.state_dict()), stale=0, epochs=[], complete=False)
        save_checkpoint(path, model, optimizer, identity=identity, optimizer_steps=0, run_state=state)
    targets = torch.tensor([r["soft_target"] for r in train["records"]], dtype=torch.float32)
    for epoch in range(state["epoch"] + 1, MAX_EPOCHS + 1):
        if state["complete"] or state["stale"] >= PATIENCE:
            break
        model.eval()
        order = list(range(len(targets)))
        random.Random(SEED ^ (0x544f5745 + epoch)).shuffle(order)
        weighted_loss, seen, max_grad = 0., 0, 0.
        for start in range(0, len(order), BATCH):
            indices = order[start:start+BATCH]
            optimizer.zero_grad(set_to_none=True)
            loss = soft_loss(model.value_head(train["value_inputs"][indices]), targets[indices])
            loss.backward()
            if any(p.grad is not None for n, p in model.named_parameters() if not n.startswith("value_head.")):
                raise ValueError("gradient escaped the value head")
            norm = torch.nn.utils.clip_grad_norm_(parameters, 1.)
            if not bool(torch.isfinite(norm)):
                raise ValueError("nonfinite tower head gradient")
            max_grad = max(max_grad, float(norm))
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            weighted_loss += float(loss.detach()) * len(indices)
            seen += len(indices)
            state["steps"] += 1
        validation_mse = mse()
        state["epoch"] = epoch
        state["epochs"].append(dict(epoch=epoch, samples=seen, batches=math.ceil(seen / BATCH),
            soft_ce=weighted_loss / seen, validation_mse=validation_mse, max_unclipped_gradient_norm=max_grad,
            ordering_sha=semantic_digest(order)))
        if validation_mse < state["best_mse"] - MIN_IMPROVEMENT:
            state.update(best_mse=validation_mse, best_epoch=epoch, best_steps=state["steps"], stale=0,
                best_head=copy.deepcopy(model.value_head.state_dict()),
                best_optimizer=copy.deepcopy(optimizer.state_dict()))
        else:
            state["stale"] += 1
        if policy_function_sha(model) != frozen:
            raise ValueError("frozen parameters or buffers changed")
        save_checkpoint(path, model, optimizer, identity=identity, optimizer_steps=state["steps"], run_state=state)
        if epoch_callback:
            epoch_callback(state["epochs"][-1])
    model.value_head.load_state_dict(state["best_head"])
    optimizer.load_state_dict(state["best_optimizer"])
    if policy_function_sha(model) != frozen or abs(mse() - state["best_mse"]) > 1e-12:
        raise ValueError("best checkpoint restoration differs")
    state["complete"] = True
    save_checkpoint(path, model, optimizer, identity=identity, optimizer_steps=state["best_steps"], run_state=state)
    return dict(status="updated" if state["best_epoch"] else "rolled_back", best_epoch=state["best_epoch"],
        initial_validation_mse=state["initial_mse"], best_validation_mse=state["best_mse"],
        epochs=state["epochs"], attempted_steps=state["steps"], selected_steps=state["best_steps"],
        frozen_sha=frozen, best_head_sha=semantic_digest(model.value_head.state_dict()),
        best_optimizer_sha=semantic_digest(optimizer.state_dict()), data_sha=data_sha,
        early_stopped=state["stale"] >= PATIENCE)


def acceptance(current, zero, mean, *, integrity):
    baseline = min(zero["mse"], mean["mse"]) if current["mse"] is not None else None
    gates = dict(mse_reduction_50pct=baseline is not None and current["mse"] <= .5 * baseline,
        mae_005=current["mae"] is not None and current["mae"] <= .05,
        direction_80pct=current["direction_accuracy"] is not None and current["direction_accuracy"] >= .8,
        coverage=current["advantage_games"] >= 4 and current["advantage_states"] >= 100,
        integrity=bool(integrity))
    return dict(passed=all(gates.values()), gates=gates, best_constant_mse=baseline,
        relative_mse_reduction=1.-current["mse"]/baseline if baseline else None)
