"""Start and verify the task-owned engine allocation on one fleet host."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shlex

from fleet_deploy import connect, inventory

ROOT = Path(__file__).resolve().parent


def start_host(host: str, inventory_path: Path, *, limit: int | None = None, workers: int = 1) -> dict:
    if not 1 <= workers <= 4:
        raise ValueError("startup workers must be between 1 and 4")
    credentials = inventory(inventory_path)
    config = json.loads((ROOT / "config.json").read_text())
    required = config["fleet"][host]
    count = required if limit is None else min(limit, required)
    project = f"/home/{credentials[host][0]}/firstlight_CR"
    task = f"artifacts/{ROOT.name}/server_engine.py"
    started = []
    def start_port(port):
        client = connect(host, credentials[host])
        try:
            command = (f"cd {shlex.quote(project)} && .venv/bin/python "
                       f"{shlex.quote(task)} start --port {port}")
            _, stdout, stderr = client.exec_command(command, timeout=240)
            output, errors = stdout.read().decode(), stderr.read().decode()
            if stdout.channel.recv_exit_status():
                raise RuntimeError(f".{host}:{port}: {errors[-600:] or output[-600:]}")
            record = json.loads(output)
            if not record.get("alive") or not record.get("ready"):
                raise RuntimeError(f".{host}:{port}: engine did not report ready")
            return port
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(start_port, port) for port in range(29920, 29920 + count)]
        for future in as_completed(futures):
            port = future.result()
            started.append(port)
            print(json.dumps({"host": host, "ready": len(started), "required": required,
                              "port": port}), flush=True)
    return {"host": host, "ready": sorted(started), "required": required}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", choices=json.loads((ROOT / "config.json").read_text())["fleet"])
    parser.add_argument("--inventory", type=Path, default=ROOT.parents[2] / "MACHINES.md")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=1)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    print(json.dumps(start_host(args.host, args.inventory, limit=args.limit, workers=args.workers)), flush=True)
