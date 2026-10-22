from collections import defaultdict
from dataclasses import asdict, dataclass
import math

import torch

from native_runner.training.fair_final.adapter import source_policy
from native_runner.training.fair_final.checkpoint import capture_rng
from native_runner.training.fair_final.learning import conditional_teacher_kl
from native_runner.training.fair_final.policy import JointPolicy
from native_runner.training.fair_migration_data import information_from_row, validate_teacher
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


SCHEMA = "FairFinalMigrationDiagnostics/v1"
THRESHOLDS = dict(exact_action_match=("min", .95), teacher_act_exact_match=("min", .90),
    mode_match=("min", .98), wdl_teacher_kl=("max", .01), value_teacher_mae=("max", .02))
METRICS = ("exact_action_match", "teacher_act_exact_match", "mode_match", "card_match",
    "cell_match", "delay_match", "cell_prefix_match", "conditional_policy_kl", "greedy_nll",
    "wdl_teacher_kl", "value_teacher_mae", "student_terminal_ce", "teacher_terminal_ce",
    "student_terminal_brier", "teacher_terminal_brier", "student_draw_probability",
    "teacher_draw_probability")
ACT_METRICS = {"teacher_act_exact_match", "card_match", "cell_match", "delay_match", "cell_prefix_match"}


@dataclass(frozen=True)
class DiagnosticRow:
    episode_id: str
    row_id: str
    owner: int
    tick: int
    teacher_action: object
    student_action: object
    metrics: dict


def phase(tick):
    return "0-60s" if tick < 1200 else "60-180s" if tick < 3600 else "180s+"


def _fp32_policy(policy):
    if not isinstance(policy, JointPolicy) or tuple(policy.temperatures) != (1., 1., 1., 1.):
        raise ValueError("unit-temperature diagnostic conditional policy required")

    def logits(prefix):
        value = policy.logits(prefix)
        if not isinstance(value, torch.Tensor) or value.dtype != torch.float32:
            raise ValueError("FP32 diagnostic conditional logits required")
        return value

    return JointPolicy(logits, policy.legality)


@torch.no_grad()
def diagnose_row(teacher_policy, student_policy, teacher_wdl, student_wdl, *,
        episode_id, row_id, owner, tick, label):
    require_sha(episode_id)
    require_sha(row_id)
    if (type(owner) is not int or owner not in (0, 1) or type(tick) is not int or tick < 0
            or label not in ("WIN", "DRAW", "LOSS")):
        raise ValueError("exact diagnostic player, tick and player-relative terminal label required")
    for logits in (teacher_wdl, student_wdl):
        if (not isinstance(logits, torch.Tensor) or logits.shape != (3,) or logits.dtype != torch.float32
                or not bool(torch.isfinite(logits).all())):
            raise ValueError("finite FP32 WDL logits in WIN/DRAW/LOSS order required")
    teacher = _fp32_policy(teacher_policy)
    student = _fp32_policy(student_policy)
    policy_kl = conditional_teacher_kl(teacher, student)
    generator = torch.Generator().manual_seed(0)
    teacher_action = teacher.sample(generator, greedy=True)[0]
    student_action = student.sample(generator, greedy=True)[0]
    teacher_act = teacher_action.card_id is not None
    exact = teacher_action == student_action
    teacher_log = teacher_wdl.double().cpu().log_softmax(-1)
    student_log = student_wdl.double().cpu().log_softmax(-1)
    teacher_probability, student_probability = teacher_log.exp(), student_log.exp()
    target = ("WIN", "DRAW", "LOSS").index(label)
    one_hot = torch.nn.functional.one_hot(torch.tensor(target), 3)
    metrics = dict(exact_action_match=float(exact), teacher_act_exact_match=float(exact) if teacher_act else None,
        mode_match=float(teacher_act == (student_action.card_id is not None)),
        card_match=float(teacher_action.card_id == student_action.card_id) if teacher_act else None,
        cell_match=float(teacher_action.cell == student_action.cell) if teacher_act else None,
        delay_match=float(teacher_action.delay_slot == student_action.delay_slot) if teacher_act else None,
        cell_prefix_match=float((teacher_action.card_id, teacher_action.cell) ==
            (student_action.card_id, student_action.cell)) if teacher_act else None,
        conditional_policy_kl=float(policy_kl), greedy_nll=float(-student.log_prob(teacher_action)),
        wdl_teacher_kl=float((teacher_probability * (teacher_log - student_log)).sum()),
        value_teacher_mae=float(abs((teacher_probability[0] - teacher_probability[2]) -
            (student_probability[0] - student_probability[2]))),
        student_terminal_ce=float(-student_log[target]), teacher_terminal_ce=float(-teacher_log[target]),
        student_terminal_brier=float(((student_probability - one_hot) ** 2).sum()),
        teacher_terminal_brier=float(((teacher_probability - one_hot) ** 2).sum()),
        student_draw_probability=float(student_probability[1]), teacher_draw_probability=float(teacher_probability[1]))
    if any(value is not None and not math.isfinite(value) for value in metrics.values()):
        raise ValueError("nonfinite diagnostic result")
    return DiagnosticRow(episode_id, row_id, owner, tick, teacher_action, student_action, metrics)


def _summary(rows):
    result = {}
    for metric in METRICS:
        by_episode = defaultdict(list)
        for row in rows:
            value = row.metrics[metric]
            if value is not None:
                by_episode[row.episode_id].append(value)
        values = [value for episode in by_episode.values() for value in episode]
        result[metric] = dict(numerator=math.fsum(values), support_rows=len(values),
            row_weighted=math.fsum(values) / len(values) if values else None,
            support_episodes=len(by_episode), episode_equal=math.fsum(
                math.fsum(episode) / len(episode) for episode in by_episode.values()) / len(by_episode)
                if by_episode else None)
    return dict(rows=len(rows), episodes=len({row.episode_id for row in rows}), metrics=result)


def summarize(rows):
    rows = list(rows)
    if not rows or len({row.row_id for row in rows}) != len(rows):
        raise ValueError("nonempty unique diagnostic decisions required")
    for row in rows:
        require_sha(row.episode_id)
        require_sha(row.row_id)
        if (type(row.owner) is not int or row.owner not in (0, 1) or type(row.tick) is not int or row.tick < 0
                or set(row.metrics) != set(METRICS)):
            raise ValueError("invalid diagnostic metadata or metrics")
        for metric, value in row.metrics.items():
            expected_none = metric in ACT_METRICS and row.teacher_action.card_id is None
            if ((value is None) != expected_none or value is not None and
                    (type(value) not in (int, float) or not math.isfinite(value))):
                raise ValueError("diagnostic metric support differs from teacher ACT population")
    groups = defaultdict(list)
    for row in rows:
        action = row.teacher_action
        keys = dict(owner=str(row.owner), phase=phase(row.tick), teacher_mode="WAIT" if action.card_id is None else "ACT",
            teacher_card="WAIT" if action.card_id is None else str(action.card_id),
            teacher_cell="WAIT" if action.cell is None else str(action.cell),
            teacher_delay="WAIT" if action.delay_slot is None else str(action.delay_slot))
        for dimension, value in keys.items():
            groups[dimension, value].append(row)
    overall = _summary(rows)
    checks = {}
    for weighting in ("row_weighted", "episode_equal"):
        metrics = overall["metrics"]
        checks[weighting] = {name: dict(observed=metrics[name][weighting], direction=direction, threshold=threshold,
            passed=None if metrics[name][weighting] is None else
                metrics[name][weighting] >= threshold if direction == "min" else metrics[name][weighting] <= threshold)
            for name, (direction, threshold) in THRESHOLDS.items()}
    return dict(schema=SCHEMA, overall=overall,
        groups={dimension: {value: _summary(group) for (axis, value), group in sorted(groups.items()) if axis == dimension}
            for dimension in sorted({axis for axis, value in groups})},
        fidelity_checks=checks, diagnostic_only=True, training_admissible=False,
        denominator_semantics=dict(act="teacher ACT; student WAIT counts as mismatch",
            component="card/cell/delay compare components; cell_prefix_match also requires same card",
            episode_equal="mean of within-episode metric means, excluding episodes with zero eligible rows",
            missing="null means no eligible support; never treated as a passing check"),
        deployment_semantics="unit-temperature sequential greedy diagnostic; not stochastic deployment strength")


@torch.no_grad()
def diagnose_validation(dataset, student, *, max_rows=None):
    teacher = dataset.teacher
    validate_teacher(teacher, dataset.store.manifest["identity"])
    if any(module.training for module in student.modules()):
        raise ValueError("diagnostic student must already be in eval mode")
    if any(parameter.dtype != torch.float32 for parameter in student.parameters()):
        raise ValueError("FP32 diagnostic student required")
    available = list(dataset.splits["validation"])
    if not available or max_rows is not None and (type(max_rows) is not int or max_rows < 1):
        raise ValueError("nonempty validation population and positive optional engineering row limit required")
    count = len(available) if max_rows is None else min(max_rows, len(available))
    selected = [available[index * len(available) // count] for index in range(count)]
    original = dict(teacher=semantic_digest(teacher.state_dict()), student=semantic_digest(student.state_dict()),
        rng=semantic_digest(capture_rng()))
    rows = []
    by_episode = defaultdict(list)
    for row_id in selected:
        match_id, offset, split = dataset.index[row_id]
        if split != "validation":
            raise ValueError("diagnostic population crosses whole-game validation split")
        by_episode[match_id].append((row_id, offset))
    try:
        for match_id, positions in by_episode.items():
            episode, receipt = dataset.store.load(match_id)
            if semantic_digest(receipt) != dataset.receipts[match_id]:
                raise ValueError("diagnostic receipt changed after dataset indexing")
            for row_id, offset in positions:
                row = episode["rows"][offset]
                if row["row_id"] != row_id:
                    raise ValueError("diagnostic row differs from frozen dataset index")
                info = information_from_row(row)
                teacher_policy, teacher_wdl = source_policy(teacher, info.frame, info.legality)
                teacher_action = teacher_policy.sample(torch.Generator().manual_seed(0), greedy=True)[0]
                if (asdict(teacher_action) != row["teacher_action"] or not torch.allclose(
                        teacher_wdl[0].softmax(-1).cpu(), row["teacher_wdl"], atol=1e-6, rtol=1e-5)):
                    raise ValueError("diagnostic teacher targets differ from recorded frozen A")
                student_policies, student_wdl = student(info.frame, [info.legality])
                rows.append(diagnose_row(teacher_policy, student_policies[0], teacher_wdl[0], student_wdl[0],
                    episode_id=match_id, row_id=row_id, owner=info.owner, tick=info.tick, label=row["wdl"]))
        report = summarize(rows)
        report.update(dataset_identity=dataset.identity, split="validation", available_rows=len(available),
            complete_validation=count == len(available), selected_row_ids=selected,
            selected_row_ids_sha256=semantic_digest(selected), teacher_network_sha256=original["teacher"],
            student_network_sha256=original["student"], engineering_subset=max_rows is not None,
            decisions=[dict(episode_id=row.episode_id, row_id=row.row_id, owner=row.owner, tick=row.tick,
                teacher_action=asdict(row.teacher_action), student_action=asdict(row.student_action),
                metrics=row.metrics) for row in rows])
        return report
    finally:
        current = dict(teacher=semantic_digest(teacher.state_dict()), student=semantic_digest(student.state_dict()),
            rng=semantic_digest(capture_rng()))
        if current != original:
            raise ValueError("diagnostic changed model state or global RNG")
