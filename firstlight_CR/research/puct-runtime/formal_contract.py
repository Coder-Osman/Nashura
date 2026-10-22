"""Fixed formal teacher and independent PPO collection allocations."""
from __future__ import annotations

import json

HOST_ORDER = ("37", "34", "35", "39", "41")
FLEET = {"34": 24, "35": 24, "37": 24, "39": 18, "41": 18}
CONCURRENCY = {"34": 2, "35": 12, "37": 12, "39": 8, "41": 8}
OPPONENTS = ("self_play", "fixed_1250", "recent")


def _ports():
    for host in HOST_ORDER:
        for offset in range(FLEET[host]):
            yield host, 29920 + offset, f"cuda:{offset % 8}"


def teacher_jobs(root, cycle: int, simulations: int = 128):
    if cycle < 1 or simulations not in (128, 256):
        raise ValueError("formal teacher cycle or simulation budget is invalid")
    jobs = []
    ports = tuple(_ports())
    for index in range(216):
        host, port, device = ports[index % len(ports)]
        ordinal = index + 1
        jobs.append({
            "host": host, "port": port, "device": device, "seed": 1755 + cycle * 10000 + ordinal,
            "episode_id": f"teacher-c{cycle:03d}-{ordinal:04d}", "simulations": simulations,
            "output": f"formal/teachers/cycle-{cycle:03d}/records/teacher-{ordinal:04d}.json",
            "log": f"formal/teachers/cycle-{cycle:03d}/logs/teacher-{ordinal:04d}.log",
            "pid": f"formal/teachers/cycle-{cycle:03d}/pids/teacher-{ordinal:04d}.pid",
        })
    return jobs


def ppo_jobs(root, update: int = 1):
    if update < 1:
        raise ValueError("formal PPO update must be positive")
    jobs = []
    ports = tuple(_ports())
    kinds = ("self_play",) * 72 + ("fixed_1250",) * 12 + ("recent",) * 24
    for index, kind in enumerate(kinds):
        host, port, device = ports[index]
        ordinal = index + 1
        destination = f"formal/ppo/update-{update:03d}/episodes/episode-{ordinal:04d}"
        jobs.append({
            "host": host, "port": port, "device": device, "seed": 1755 + update * 10000 + 5000 + ordinal,
            "episode_id": f"ppo-u{update:03d}-{ordinal:04d}", "opponent_kind": kind,
            "destination": destination,
            "log": f"formal/ppo/update-{update:03d}/logs/episode-{ordinal:04d}.log",
            "pid": f"formal/ppo/update-{update:03d}/pids/episode-{ordinal:04d}.pid",
        })
    return jobs


def config(root):
    return json.loads((root / "config.json").read_text())
