"""Read-only five-host inventory; credentials stay in the existing local inventory."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

ROOT = Path(__file__).resolve().parent
# Public host keys corroborated against the existing trusted .34/.37 known_hosts.
HOST_KEYS = {
    "35": "AAAAC3NzaC1lZDI1NTE5AAAAIP/mcP+tbtkVI0Hvl5Y4s+L97EpBSR6l4d1eigUv4myg",
    "39": "AAAAC3NzaC1lZDI1NTE5AAAAICnnMLM7bM7akCUv63hZVYYVGru75CtM/xXHDGfL0/av",
    "41": "AAAAC3NzaC1lZDI1NTE5AAAAILLZ3UVjM+9p3dPo1JzBMha04U0DtgJlCnlrne2tKZFc",
}
RUNTIME_FILES = (
    "config.json",
    "server_engine.py",
    "../standalone-resident-20260927/build-manifest.json",
    "../standalone-resident-20260927/libresident.so",
    "../standalone-resident-20260927/libbootstrap.so",
    "../render-web/weights/e9f7c84a3e5f84a4d625c3f9a68e332a318a2aedc06e196da55bdb00a66bd1f0.pt",
)
PROBE = '''import hashlib, json, shutil, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

def probe_engine(service, record, port, owned_alive):
    result = {"port": port, "ready": False}
    try:
        if not record.get("ready") or not owned_alive(service, record, port):
            raise RuntimeError("task-owned supervisor is not ready")
        child = {"pid": record.get("child_pid"), "start_ticks": record.get("child_start_ticks")}
        if not service.alive(child):
            raise RuntimeError("native child process identity is not live")
        status = service.request(port, "status")
        if not isinstance(status, dict):
            raise RuntimeError("invalid native status response")
        result.update(cold_ready=status.get("coldReady"), content_ready=status.get("contentReady"),
                      snapshot_handles=status.get("snapshotHandles"), tick=status.get("tick"),
                      native_mode=status.get("mode"))
        if status.get("ok") is not True or status.get("coldReady") is not True or status.get("contentReady") is not True:
            raise RuntimeError("native content is not ready")
        result["ready"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result

def probe_host():
    task = Path("artifacts/hog-tree-search-1755-200r-20260929").resolve()
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    result = {"project": str(Path.cwd()), "task_present": task.is_dir(),
              "disk_free_gib": shutil.disk_usage(".").free / 2**30,
              "memory_available_gib": int(memory["MemAvailable"].split()[0]) / 1024**2,
              "resident_present": (task.parent / "standalone-resident-20260927/service.py").is_file(),
              "stop_markers": [name for name in ("STOP", "BOUNDARY_STOP") if (task / name).exists()],
              "ready_ports": [], "engines": [], "runtime_hashes": {}}
    config = json.loads((task / "config.json").read_text()) if result["task_present"] else {}
    result["required_task_engines"] = config.get("fleet", {}).get(host, 0)
    for name in runtime_files:
        filename = task / name
        if not filename.is_file():
            result["runtime_hashes"][name] = None
            continue
        digest = hashlib.sha256()
        with filename.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result["runtime_hashes"][name] = digest.hexdigest()
    if result["task_present"] and result["resident_present"]:
        sys.path.insert(0, str(task))
        from server_engine import runtime, owned_alive
        service = runtime()
        def inspect_port(port):
            record = service.load(task / "engines" / str(port) / "process.json")
            return probe_engine(service, record, port, owned_alive)
        ports = range(29920, 29920 + result["required_task_engines"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            result["engines"] = list(pool.map(inspect_port, ports))
        result["ready_ports"] = [entry["port"] for entry in result["engines"] if entry["ready"]]
    result["ready_task_engines"] = len(result["ready_ports"])
    return result

if __name__ == "__main__":
    print(json.dumps(probe_host()))
'''


def runtime_hashes() -> dict[str, str]:
    result = {}
    for name in RUNTIME_FILES:
        with (ROOT / name).open("rb") as stream:
            result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def readiness_blockers(report, config, expected_hashes, *, now=None):
    errors = []
    checked_at = report.get("checked_at", 0)
    age = (time.time() if now is None else now) - checked_at
    if not checked_at or age < 0 or age > 300:
        errors.append("five-host preflight report is not fresh")
    for host, required in config["fleet"].items():
        state = report.get("hosts", {}).get(host, {})
        if state.get("error"):
            errors.append(f".{host}: {state['error']}")
        for field in ("task_present", "resident_present"):
            if state.get(field) is not True:
                errors.append(f".{host}: {field} is false")
        if state.get("stop_markers"):
            errors.append(f".{host}: task stop marker is present")
        for field, minimum in (("disk_free_gib", 20), ("memory_available_gib", 32)):
            if state.get(field, 0) < minimum:
                errors.append(f".{host}: {field} is below {minimum} GiB")
        expected_ports = list(range(29920, 29920 + required))
        if state.get("ready_ports") != expected_ports or state.get("ready_task_engines") != required:
            errors.append(f".{host}: {state.get('ready_task_engines', 0)}/{required} task engines respond ready")
        for name, expected in expected_hashes.items():
            if state.get("runtime_hashes", {}).get(name) != expected:
                errors.append(f".{host}: runtime hash mismatch: {name}")
    return errors


def collect(inventory):
    rows = Path(inventory).read_text().splitlines()
    config = json.loads((ROOT / "config.json").read_text())
    def probe(host):
        ip = "172.16.48." + host
        row = next(line for line in rows if line.startswith("|") and f"| {ip} |" in line)
        fields = [field.strip() for field in row.split("|")]
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if host in HOST_KEYS:
            client.get_host_keys().add(ip, "ssh-ed25519", paramiko.Ed25519Key(
                data=base64.b64decode(HOST_KEYS[host])))
        try:
            client.connect(ip, username=fields[3], password=fields[4], timeout=8)
            project = f"/home/{fields[3]}/firstlight_CR"
            code = f"host={host!r}\nruntime_files={RUNTIME_FILES!r}\n" + PROBE
            _, stdout, stderr = client.exec_command(
                "cd " + shlex.quote(project) + " && .venv/bin/python -c " + shlex.quote(code), timeout=120)
            output, errors = stdout.read().decode(), stderr.read().decode()
            if stdout.channel.recv_exit_status():
                raise RuntimeError(errors[-1000:])
            return host, json.loads(output)
        except Exception as exc:
            return host, dict(error=str(exc), ready_task_engines=0)
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=5) as pool:
        hosts = dict(pool.map(probe, config["fleet"]))
    result = dict(checked_at=time.time(), hosts=hosts, expected_fleet=config["fleet"], read_only=True)
    result["expected_runtime_hashes"] = runtime_hashes()
    result["blockers"] = readiness_blockers(result, config, result["expected_runtime_hashes"])
    result["passed"] = not result["blockers"]
    result["ready_task_engines"] = sum(state["ready_task_engines"] for state in hosts.values())
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=ROOT.parents[2] / "MACHINES.md")
    parser.add_argument("--output", type=Path, default=ROOT / "fleet-preflight.json")
    args = parser.parse_args()
    result = collect(args.inventory)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
