import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .storage import atomic_write
from .value_collect import CoordinatorClient
from .value_capacity import actor_device, capacity_config


def resources(root, host):
    memory = {line.split(":")[0]: int(line.split()[1])
              for line in Path("/proc/meminfo").read_text().splitlines()}
    swaps = {line.split()[0]: int(line.split()[1])
             for line in Path("/proc/vmstat").read_text().splitlines()
             if line.startswith(("pswpin ", "pswpout "))}
    return dict(host=host, available_gb=memory["MemAvailable"] / 2**20,
                disk_gb=shutil.disk_usage(root).free / 2**30, **swaps)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", required=True)
    args = parser.parse_args(argv)
    root = args.root
    client = CoordinatorClient(root)
    active = {}
    stopping = False

    def stop(*unused):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with (root / "host.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            while not stopping:
                for slot, process in list(active.items()):
                    if process.poll() is not None:
                        client.request("/actor-exit", dict(host=args.host, slot=slot,
                                                          exit_code=process.returncode))
                        atomic_write(root / "actors" / f"slot-{slot}" / "exit.json",
                                     json.dumps(dict(pid=process.pid, exit_code=process.returncode,
                                                     ended=time.time())).encode(), replace=True)
                        del active[slot]
                sample = resources(root, args.host)
                with (root / "resources.jsonl").open("a") as output:
                    output.write(json.dumps(dict(time=time.time(), **sample)) + "\n")
                try:
                    status = client.request("/heartbeat", sample)
                except OSError as error:
                    atomic_write(root / "network-error.json", json.dumps(dict(error=repr(error),
                                 time=time.time())).encode(), replace=True)
                    time.sleep(10)
                    continue
                atomic_write(root / "host-status.json", json.dumps(dict(status=status,
                             actors={slot: process.pid for slot, process in active.items()})).encode(), replace=True)
                if status["finished"]:
                    break
                for slot in range(status["slots"]):
                    if slot in active:
                        continue
                    folder = root / "actors" / f"slot-{slot}"
                    folder.mkdir(parents=True, exist_ok=True)
                    device = actor_device(capacity_config(root), args.host, slot)
                    environment = dict(os.environ, CUDA_VISIBLE_DEVICES=str(device), OMP_NUM_THREADS="1",
                                       MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
                    with (folder / "worker.log").open("a") as output:
                        process = subprocess.Popen([sys.executable, "-u", "-m",
                                                    "native_runner.training.fair_search.value_collect",
                                                    "--root", str(root), "--host", args.host,
                                                    "--slot", str(slot)], cwd=root / "project", env=environment,
                                                   stdout=output, stderr=subprocess.STDOUT)
                    active[slot] = process
                    atomic_write(folder / "launch.json", json.dumps(dict(pid=process.pid, gpu=device,
                                 started=time.time())).encode(), replace=True)
                time.sleep(10)
        finally:
            for process in active.values():
                process.terminate()
            for process in active.values():
                try:
                    process.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    main()
