"""Generate and render one match per deck pairing from a single event checkpoint.

Everything remote goes through paramiko/sftp (the ssh binary from this Mac keeps
hanging on heredocs); the local render runs through tools/replay_driver.py.

usage:
    python3 tools/demo_match_set.py <checkpoint-on-remote> <label> [seed]
        [--source user@host:/path/event-pretrain.pt]   # stage if absent
        [--env "CR_EVENT_MODEL_SNAPSHOT_ONLY=1 CR_EVENT_MODEL_SCALE=1.85"]
        [--host user@host] [--repo path] [--data-root path] [--gen-only]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import paramiko

PAIRS = (
    ("golem-vs-golem", "golem-beatdown", "golem-beatdown", 28489),
    ("golem-vs-hog", "golem-beatdown", "hog-cycle", 28589),
    ("hog-vs-hog", "hog-cycle", "hog-cycle", 28689),
)
LOCAL_ROOT = Path("/Users/gujiga/Documents/ChatGPT/CR")
ADB = "/Users/gujiga/Library/Android/sdk/platform-tools/adb"


def connect(host_spec: str, password: str | None):
    user, host = host_spec.split("@")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if password:
        client.connect(host, username=user, password=password, timeout=20)
    else:
        client.connect(host, username=user, timeout=20)
    return client


def run(client, command: str, timeout: int = 300) -> str:
    _, out, err = client.exec_command(command, timeout=timeout)
    return (out.read().decode(errors="replace") + err.read().decode(errors="replace")).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkpoint")
    parser.add_argument("label")
    parser.add_argument("seed", nargs="?", type=int, default=20260926)
    parser.add_argument("--source", default="", help="user@host:/path to stage from")
    parser.add_argument("--env", default="", help="contract env prefix for the generator")
    parser.add_argument("--host", default="zhichun@172.16.48.37")
    parser.add_argument("--password", default=os.environ.get("CR_REMOTE_PASSWORD", ""))
    parser.add_argument("--repo", default="/data/firstlight/home/firstlight_CR")
    parser.add_argument("--data-root", default="/data/firstlight")
    parser.add_argument("--gen-only", action="store_true")
    args = parser.parse_args()

    client = connect(args.host, args.password)
    staged = args.checkpoint

    print("== 1/4 checkpoint ==")
    if run(client, f"test -f {args.checkpoint} && echo yes") != "yes":
        if not args.source:
            print("  远端没有该 checkpoint，且未提供 --source")
            return 1
        src_host, src_path = args.source.split(":", 1)
        staged = f"{args.data_root}/ckpt-{args.label}.pt"
        with paramiko.SSHClient() as src:
            src.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            src.connect(src_host.split("@")[1], username=src_host.split("@")[0],
                        password=args.password, timeout=20)
            with src.open_sftp() as sftp:
                sftp.get(src_path, f"/tmp/ckpt-{args.label}.pt")
        with client.open_sftp() as sftp:
            sftp.put(f"/tmp/ckpt-{args.label}.pt", staged)
        local_md5 = subprocess.run(["md5", "-q", f"/tmp/ckpt-{args.label}.pt"],
                                   capture_output=True, text=True).stdout.strip()
        remote_md5 = run(client, f"md5sum {staged} | cut -c1-32")
        if local_md5 != remote_md5:
            print(f"  md5 不一致: {local_md5} vs {remote_md5}")
            return 1
        print(f"  已暂存 {staged} (md5 {local_md5[:12]}…)")
    else:
        print(f"  使用远端已有 {staged}")
    print(f"  契约: '{args.env or '无 = 旧契约'}'")

    print("== 2/4 三局并行生成 ==")
    for name, deck0, deck1, port in PAIRS:
        out_dir = f"{args.data_root}/demo-{args.label}-{name}"
        command = (
            f"cd {args.repo} && rm -rf {out_dir} && mkdir -p {out_dir} && "
            f"{args.env} setsid nohup .venv/bin/python -u tools/demo_event_selfplay.py "
            f"--checkpoint {staged} --deck0 {deck0} --deck1 {deck1} --seed {args.seed} "
            f"--out-dir {out_dir} --port {port} --env-id 0 --device cpu "
            f"--max-ticks 5800 --max-seconds 1200 --max-wait-ticks 60 "
            f"> {out_dir}/gen.log 2>&1 < /dev/null & echo started"
        )
        try:
            run(client, command, timeout=25)
        except Exception as error:  # noqa: BLE001 - the channel may stay open on the detached child
            print(f"  {name}: 启动调用返回 {type(error).__name__}（进程已 detach，继续）")
        print(f"  {name}: {deck0} vs {deck1} (port {port})", flush=True)

    print("== 3/4 等生成完成 ==")
    for _ in range(180):
        counts = run(client, f"ls {args.data_root}/demo-{args.label}-*/stats.json 2>/dev/null | wc -l")
        if counts.strip() == "3":
            break
        time.sleep(10)
    else:
        print("  超时：生成未完成")
        return 1

    local_dir = LOCAL_ROOT / f"demo-{args.label}"
    local_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    with client.open_sftp() as sftp:
        for name, _d0, _d1, _port in PAIRS:
            for suffix in ("trajectory.jsonl", "render-match.json", "stats.json"):
                sftp.get(f"{args.data_root}/demo-{args.label}-{name}/{suffix}",
                         str(local_dir / f"{name}-{suffix}"))
            stats = json.loads((local_dir / f"{name}-stats.json").read_text())
            result = stats["result"]
            summary.append((name, stats, result))
            print(f"  {name}: 出牌 {stats['scheduled_plays']} tick {result['tick']} "
                  f"胜者 {result['winner']} 皇冠 {result['crowns']} 生成 {stats['wall_seconds']}s")

    if args.gen_only:
        print(f"== 生成完成（未渲染）: {local_dir} ==")
        return 0

    print("== 4/4 本机依次渲染 ==")
    for name, stats, result in summary:
        seconds = int(result["tick"] / 20) + 25
        record = f"/sdcard/{args.label}-{name}.mp4"
        subprocess.run([ADB, "shell", "rm", "-f", record], capture_output=True, timeout=60)
        print(f"  --- {name}: 录制上限 {seconds}s ---", flush=True)
        proc = subprocess.run(
            [sys.executable, str(LOCAL_ROOT / "firstlight_CR/tools/replay_driver.py"),
             "--trajectory", str(local_dir / f"{name}-trajectory.jsonl"),
             "--match-json", str(local_dir / f"{name}-render-match.json"),
             "--record", record, "--record-limit", str(seconds), "--max-tick", "5800"],
            capture_output=True, text=True, timeout=seconds + 240,
        )
        tail = [line for line in (proc.stdout + proc.stderr).splitlines() if "schedule_outcome" in line]
        if tail:
            print(f"    {tail[-1][:200]}")
        subprocess.run([ADB, "pull", record, str(local_dir / f"{name}.mp4")],
                       capture_output=True, timeout=300)
        video = local_dir / f"{name}.mp4"
        if video.is_file():
            print(f"    {video}  {video.stat().st_size / 1048576:.1f} MB")
    print(f"完成: {local_dir}")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
