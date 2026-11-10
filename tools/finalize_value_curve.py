"""Audit a completed value curve and publish aggregate results, without evaluation."""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "firstlight_CR"))

import torch

from native_runner.training.sspi import value_curve as curve
from tools.export_public_snapshot import export


REPORT = "docs/SSPI_VALUE_CURVE_V4_2026-11-10.md"
RESULTS = "docs/SSPI_VALUE_CURVE_V4_RESULTS_2026-11-10.json"


def _command(args, *, log=None):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False,
        timeout=1200)
    if log is not None:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args[0]}\n{result.stderr[-4000:]}")
    return result.stdout.strip()


def _state(run_dir, stage, **details):
    curve._atomic_json(run_dir / "finalizer_status.json", dict(stage=stage,
        updated_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), **details))


def audit(run_dir, *, preview=False):
    config = json.loads((run_dir / "run_config.json").read_text())
    status = json.loads((run_dir / "status.json").read_text())
    if not preview and (status.get("status") != "complete" or status.get("stage") != "finished"):
        raise RuntimeError("final publication requires the completed collection/update/holdout run")
    checks = curve._completed_checks(run_dir, config)
    if not checks:
        raise ValueError("no committed checkpoint is available")
    for check in checks:
        saved = curve._read_checkpoint_payload(run_dir / "checkpoints"
            / f"curve-{check['training_games']:04d}.pt", config["checkpoint_identity"])
        summary = saved["run_state"].get("last_check_summary")
        if summary is not None and summary != {key: value for key, value in check.items()
                if key != "checkpoint"}:
            raise ValueError("result JSON differs from the checkpoint's committed summary")
        if (saved["optimizer_sha"] != check["checkpoint"]["optimizer_sha256"]
                or saved["run_state_sha"] != check["checkpoint"]["run_state_sha256"]
                or curve.semantic_digest({key: value for key, value in saved["model"].items()
                    if not key.startswith("value_head.")}) != config["initial_policy_function_sha"]):
            raise ValueError("checkpoint optimizer, cursor or frozen function differs")
        if summary is None and (check.get("recovery", {}).get("kind") != "recovered_committed_update"
                or check.get("recovery", {}).get("optimizer_steps_repeated") != 0):
            raise ValueError("checkpoint without a summary has no audited recovery record")
    holdout_path = run_dir / "final-holdout.json"
    holdout = json.loads(holdout_path.read_text()) if holdout_path.exists() else None
    last = checks[-1]
    checkpoint = run_dir / "checkpoints" / f"curve-{last['training_games']:04d}.pt"
    payload = curve._read_checkpoint_payload(checkpoint, config["checkpoint_identity"])
    frozen_sha = curve.semantic_digest({key: value for key, value in payload["model"].items()
        if not key.startswith("value_head.")})
    if frozen_sha != config["initial_policy_function_sha"]:
        raise ValueError("final checkpoint changed frozen parameters or buffers")
    signals = [check for check in checks if check["evaluation"]["validation_signal"]]
    if len(signals) > 1 or signals and signals[-1] is not last:
        raise ValueError("training continued past the first validation signal")
    if not preview:
        if (not holdout or holdout.get("schema") != curve.HOLDOUT_SCHEMA
                or holdout.get("run_id") != config["run_id"]
                or holdout.get("checkpoint_sha256") != curve._file_sha(checkpoint)
                or holdout.get("policy_function_sha") != frozen_sha
                or holdout.get("training_games") != last["training_games"]
                or holdout.get("optimizer_steps") != payload["optimizer_steps"]
                or curve._file_sha(run_dir / "checkpoints" / "current.pt") != curve._file_sha(checkpoint)):
            raise ValueError("final holdout and checkpoint identities differ")
        if not signals and last["training_games"] != curve.MAX_TRAIN_GAMES:
            raise ValueError("run stopped before a signal or the preregistered upper bound")
    episode_shas = {}
    for check in checks:
        episode_shas.update(check["current_batch"]["episode_sha256"])
    episode_shas.update(last["validation"]["episode_sha256"])
    if holdout is not None:
        episode_shas.update(holdout["games"]["episode_sha256"])
    stats = {}
    for split, count in (("training", last["training_games"]), ("validation", 32),
            ("holdout", 32 if holdout is not None else 0)):
        episodes = []
        for index in range(1, count + 1):
            game_id = curve._game_id(split, index)
            directory = run_dir / "episodes" / game_id
            metadata = json.loads((directory / "episode.json").read_text())
            identity = metadata["identity"]
            if ((directory / "quarantine.json").exists() or metadata["status"] != "complete"
                    or curve._file_sha(directory / "episode.json") != episode_shas[game_id]
                    or identity["run_id"] != config["run_id"] or identity["game_id"] != game_id
                    or identity["seed"] != curve._seed_for(split, index)
                    or identity["source_sha256"] != curve._episode_source(config, split, index)
                    or identity["engine_sha256"] != config["engine_sha256"]
                    or identity["model_sha256_by_owner"] != {"0": config["initial_model_sha"], "1": config["initial_model_sha"]}
                    or metadata["search_roots"] != 0 or metadata["search_probability"] != 0.
                    or curve._file_sha(directory / "actions.json") != metadata["actions_sha256"]
                    or curve._file_sha(directory / "episode.pt") != metadata["tensor_sha256"]):
                raise ValueError(f"episode evidence differs at {game_id}")
            actions = json.loads((directory / "actions.json").read_text())["rows"]
            if any(row["mode"] != "DIRECT" for row in actions):
                raise ValueError("final audit found a non-direct decision")
            episodes.append(metadata)
        if episodes:
            seconds = sum(episode["elapsed_seconds"] for episode in episodes)
            stats[split] = dict(collected_games=count,
                decisive_games=sum(curve.decisive_result(episode["result"]) for episode in episodes),
                excluded_draw_games=sum(not curve.decisive_result(episode["result"]) for episode in episodes),
                sampled_states=sum(episode["value_samples"] for episode in episodes),
                search_roots=sum(episode["search_roots"] for episode in episodes),
                episode_seconds_total=seconds, episode_seconds_mean=seconds / count)
    if stats["training"]["sampled_states"] != last["sampled_training_rows"]:
        raise ValueError("cumulative training exposure differs from the episodes")
    if not preview:
        expected_games = set(episode_shas)
        actual_games = {path.parent.name for path in (run_dir / "episodes").glob("*/episode.json")}
        if expected_games != actual_games or list((run_dir / "episodes").glob("*/quarantine.json")):
            raise ValueError("unaccounted complete or quarantined episodes require inspection")
    return dict(config=config, checks=checks, holdout=holdout, collection=stats,
        complete=not preview, run_status=status.get("status"), validation_signal=bool(signals),
        confirmed=bool(signals and holdout and holdout["status"] == "confirmed"))


def _public_check(record):
    result = dict(record, checkpoint=dict(record["checkpoint"],
        path=Path(record["checkpoint"]["path"]).name))
    result["current_batch"] = {key: value for key, value in record["current_batch"].items()
        if key not in ("episode_sha256", "training_games")}
    result["current_batch"]["training_games"] = {key: value
        for key, value in record["current_batch"]["training_games"].items() if key != "episode_sha256"}
    result["validation"] = {key: value for key, value in record["validation"].items()
        if key != "episode_sha256"}
    return result


def render(audited):
    last = audited["checks"][-1]
    if not audited["complete"]:
        outcome = ("试验已失败，等待检查证据" if audited.get("run_status") == "failed"
            else "运行中，最终留出尚未确认")
    elif audited["confirmed"]:
        outcome = f"首次在{last['training_games']}训练局达到验证门槛，并获32局独立留出确认"
    elif audited["validation_signal"]:
        outcome = f"{last['training_games']}局出现验证信号，最终留出未确认"
    else:
        outcome = "至1,024训练局未出现校正后的验证信号；已评估上限模型"
        outcome += ("，上限模型的留出比较通过" if audited["holdout"]
            and audited["holdout"]["status"] == "confirmed" else "，留出未确认改善")
    metrics = last["evaluation"]["current"]
    validation_ce = f"{metrics['wl_ce']:.8f}" if metrics is not None else "未定义"
    summary = (f"{outcome}。累计{last['sampled_training_rows']:,}个训练状态、"
        f"{last['checkpoint']['optimizer_steps']}步AdamW；验证逐状态WL CE {validation_ce}。"
        "共享主体、策略头及缓冲区SHA保持不变，结果只评价价值预测，不表示策略胜率提升。")
    lines = ["| 训练局 | 有效胜负局 | 累计状态 | 验证CE | Brier | ECE10 | 按局CE差：初始 / 常数 | 验证达标 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"]
    for check in audited["checks"]:
        evaluation = check["evaluation"]
        current = evaluation["current"]
        if current is None:
            lines.append(f"| {check['training_games']} | {check['decisive_training_games']} | {check['sampled_training_rows']} | — | — | — | 指标未定义 | 否 |")
            continue
        diffs = [evaluation[key] for key in ("current_minus_initial_ce", "current_minus_constant_ce")]
        differences = " / ".join(f"{value['mean']:+.6f}" if value else "未定义" for value in diffs)
        lines.append(f"| {check['training_games']} | {check['decisive_training_games']} | {check['sampled_training_rows']:,} | {current['wl_ce']:.8f} | {current['brier']:.8f} | {current['ece10']:.8f} | {differences} | {'是' if evaluation['validation_signal'] else '否'} |")
    holdout = audited["holdout"]
    if holdout is not None and holdout.get("current") is not None:
        current = holdout["current"]
        lines.extend(["", f"最终留出：{holdout['games']['decisive_games']}/32个有效胜负局、{holdout['selected_rows']:,}个状态；"
            f"WL CE {current['wl_ce']:.8f}、Brier {current['brier']:.8f}、ECE10 {current['ece10']:.8f}。"])
        for name, key in (("初始模型", "current_minus_initial_ce"), ("训练频率常数", "current_minus_constant_ce")):
            difference = holdout[key]
            lines.append(f"相对{name}的按局CE差{difference['mean']:+.8f}，95%区间[{difference['ci95'][0]:+.8f}, {difference['ci95'][1]:+.8f}]，两项比较校正后的单侧上界{difference['upper']:+.8f}。")
    return summary, "\n".join(lines)


def write_reports(run_dir, audited, *, preview):
    summary, table = render(audited)
    public = json.loads((ROOT / RESULTS).read_text())
    if public["run_id"] != audited["config"]["run_id"]:
        raise ValueError("publication report belongs to another experiment run")
    public.update(status="complete" if audited["complete"] else "running",
        snapshot_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        checks=[_public_check(record) for record in audited["checks"]],
        collection_all_complete=audited["collection"],
        first_validation_signal_training_games=(audited["checks"][-1]["training_games"]
            if audited["validation_signal"] else None),
        first_holdout_confirmed_training_games=(audited["checks"][-1]["training_games"]
            if audited["confirmed"] else None), conclusion=summary)
    if audited["holdout"] is not None:
        public["final_holdout"] = dict(audited["holdout"], games={key: value
            for key, value in audited["holdout"]["games"].items() if key != "episode_sha256"})
    for path in sorted((run_dir / "checks").glob("check-*.json")):
        if path.name not in {Path(item["file"]).name for item in public["source_records"]}:
            public["source_records"].append(dict(file="checks/" + path.name,
                sha256=curve._file_sha(path), bundled=False))
    if (run_dir / "environment.json").exists():
        public["environment"] = json.loads((run_dir / "environment.json").read_text())
    if audited["holdout"] is not None:
        public["source_records"].append(dict(file="final-holdout.json",
            sha256=curve._file_sha(run_dir / "final-holdout.json"), bundled=False))
    if preview:
        curve._atomic_json(run_dir / "publication-preview.json", public)
        (run_dir / "publication-preview.md").write_text(summary + "\n\n" + table + "\n")
        return summary
    curve._atomic_json(ROOT / RESULTS, public)
    report = (ROOT / REPORT).read_text()
    beginning = report.split("## 当前进度", 1)[0]
    recovery = report.split("## 运行修复与恢复边界", 1)[1]
    collections = "\n".join(f"- {split}：{stats['collected_games']}采集局、{stats['decisive_games']}有效胜负局、"
        f"{stats['excluded_draw_games']}排除平局、{stats['sampled_states']:,}状态；episode平均{stats['episode_seconds_mean']:.3f}秒。"
        for split, stats in audited["collection"].items())
    (ROOT / REPORT).write_text(beginning + "## 当前进度\n\n" + summary + "\n\n" + collections
        + "\n\n## 结果\n\n" + table + "\n\n"
        + "表内CE、Brier和ECE为逐状态均值；显著性比较采用完整对局等权CE差。全部置信区间、校正上界、模型/AdamW SHA及原始私有记录SHA见[机器可读摘要](SSPI_VALUE_CURVE_V4_RESULTS_2026-11-10.json)。"
        + "最终留出只读取既有评估记录，本汇总不会重新评估模型。\n\n"
        + "主工程511项、公开检查1,104项通过（42项跳过）；最终发布重新执行公开快照、主工程、公开检查与gitleaks。"
        + "初始64局的4个真实状态25个策略条件分支log概率差为0，之后各检查点继续验证完整冻结函数SHA。\n\n"
        + "## 运行修复与恢复边界" + recovery)
    for name, following in (("README.md", "## 2026-11-10：SSPI AlphaStar"),
            ("docs/RESULTS.md", "## 2026-11-10：SSPI AlphaStar")):
        text = (ROOT / name).read_text()
        leading = text.split("## 2026-11-10：v4", 1)[0]
        rest = following + text.split(following, 1)[1]
        section = "## 2026-11-10：v4 直出纯价值头样本效率试验（已完成）\n\n" + summary
        section += "\n\n实现、故障恢复、全部检查点及独立留出记录见[v4学习曲线报告]("
        section += ("docs/" if name == "README.md" else "") + "SSPI_VALUE_CURVE_V4_2026-11-10.md)。\n\n"
        (ROOT / name).write_text(leading + section + rest)
    changelog = (ROOT / "CHANGELOG.md").read_text()
    marker = "## 2026-11-10：v4 直出纯价值头样本效率试验\n\n"
    changelog = changelog.replace(marker, marker + "- 完成预登记试验：" + summary + "\n", 1)
    (ROOT / "CHANGELOG.md").write_text(changelog)
    return summary


def main():
    torch.set_num_threads(1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--expected-head")
    parser.add_argument("--export-dir", type=Path)
    parser.add_argument("--git-proxy")
    parser.add_argument("--orchestrator-pid", type=int)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    lock = (run_dir / "publication.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        if not args.preview and (not args.expected_head or args.export_dir is None):
            raise ValueError("finalization requires a reviewed HEAD and isolated export directory")
        if args.wait:
            while True:
                status = json.loads((run_dir / "status.json").read_text())
                if status.get("status") == "complete":
                    break
                if status.get("status") == "failed":
                    raise RuntimeError("training failed; inspect preserved evidence before resuming")
                if args.orchestrator_pid is not None:
                    process = subprocess.run(["ps", "-p", str(args.orchestrator_pid),
                        "-o", "command="], text=True, capture_output=True, check=False)
                    if (process.returncode or "native_runner.training.sspi.value_curve run" not in process.stdout
                            or str(run_dir) not in process.stdout):
                        raise RuntimeError("orchestrator command identity disappeared before completion")
                _state(run_dir, "waiting_for_complete_run", training_status=status.get("stage"))
                time.sleep(15)
        if not args.preview:
            if not args.expected_head or _command(["git", "rev-parse", "HEAD"]) != args.expected_head:
                raise RuntimeError("publication checkout HEAD differs from the reviewed starting commit")
            if _command(["git", "status", "--porcelain"]):
                raise RuntimeError("publication checkout has uncommitted work; preserve it for review")
        _state(run_dir, "auditing")
        audited = audit(run_dir, preview=args.preview)
        summary = write_reports(run_dir, audited, preview=args.preview)
        if args.preview:
            _state(run_dir, "preview_ready", summary=summary)
            print(summary, flush=True)
            return
        if args.export_dir is None or not args.export_dir.is_dir():
            raise ValueError("an existing isolated publication export directory is required")
        log = run_dir / "final-publication.log"
        _state(run_dir, "engineering_checks")
        main_output = _command([sys.executable, "tools/run_main_engine_checks.py"], log=log)
        public_output = _command([sys.executable, "tools/run_public_checks.py"], log=log)
        main_passed = sum(map(int, re.findall(r"\b(\d+) passed\b", main_output)))
        public_passed = sum(map(int, re.findall(r"\b(\d+) passed\b", public_output)))
        public_skipped = sum(map(int, re.findall(r"\b(\d+) skipped\b", public_output)))
        if (main_passed, public_passed, public_skipped) != (511, 1104, 42):
            raise RuntimeError("check counts changed; review the generated summary before publishing")
        export(ROOT, args.export_dir.resolve())
        shutil.copyfile(args.export_dir / "publication/source-manifest.json", ROOT / "publication/source-manifest.json")
        _command([sys.executable, "tools/check_public_snapshot.py"], log=log)
        _command(["gitleaks", "dir", ".", "--no-banner", "--redact"], log=log)
        _command(["git", "diff", "--check"], log=log)
        if args.publish:
            _state(run_dir, "publishing")
            files = ["README.md", "CHANGELOG.md", "docs/RESULTS.md", REPORT, RESULTS,
                "publication/source-manifest.json"]
            files.extend(str(path.relative_to(ROOT)) for path in (ROOT / "publication").glob("checks-*.xml"))
            _command(["git", "add", "--", *files], log=log)
            _command(["git", "diff", "--cached", "--check"], log=log)
            _command(["git", "commit", "-m", "Report completed v4 value-head sample-efficiency experiment"], log=log)
            git = ["git"] + (["-c", "http.proxy=" + args.git_proxy] if args.git_proxy else [])
            _command([*git, "push", "origin", "HEAD:main"], log=log)
            commit = _command(["git", "rev-parse", "HEAD"])
            remote = _command([*git, "ls-remote", "origin", "refs/heads/main"]).split()[0]
            if remote != commit:
                raise RuntimeError("remote main does not match the published result commit")
            _state(run_dir, "published", commit=commit, summary=summary)
        else:
            _state(run_dir, "ready_for_review", summary=summary)
    except Exception as error:
        _state(run_dir, "needs_inspection", exception=type(error).__name__, reason=str(error))
        raise


if __name__ == "__main__":
    main()
