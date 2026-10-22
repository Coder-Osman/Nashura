"""Copy the verified Linux runtime from .37 to the task's fleet hosts."""
from __future__ import annotations

import argparse
import base64
import hashlib
from pathlib import Path
import shlex

import paramiko

from fleet_preflight import HOST_KEYS

ROOT = Path(__file__).resolve().parent
TASK = ROOT.name
ENGINE = "standalone-engine-20260926"
RESIDENT = "standalone-resident-20260927"
CHECKPOINT = "e9f7c84a3e5f84a4d625c3f9a68e332a318a2aedc06e196da55bdb00a66bd1f0.pt"
SOURCE_HOST = "37"
DESTINATIONS = ("34", "35", "39", "41")
FILES = (
    f"artifacts/{ENGINE}/qemu/usr/bin/qemu-aarch64-static",
    f"artifacts/{RESIDENT}/service.py",
    f"artifacts/{TASK}/server_engine.py",
    f"artifacts/{TASK}/config.json",
    f"artifacts/render-web/weights/{CHECKPOINT}",
)
SERVICE_PATH = f"artifacts/{RESIDENT}/service.py"
TASK_CONFIG_PATH = f"artifacts/{TASK}/config.json"


def inventory(path: Path) -> dict[str, tuple[str, str]]:
    rows = path.read_text().splitlines()
    result = {}
    for host in (SOURCE_HOST, *DESTINATIONS):
        ip = f"172.16.48.{host}"
        row = next(line for line in rows if line.startswith("|") and f"| {ip} |" in line)
        fields = [field.strip() for field in row.split("|")]
        result[host] = fields[3], fields[4]
    return result


def connect(host: str, credentials: tuple[str, str]):
    ip = f"172.16.48.{host}"
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if host in HOST_KEYS:
        client.get_host_keys().add(
            ip, "ssh-ed25519", paramiko.Ed25519Key(data=base64.b64decode(HOST_KEYS[host]))
        )
    client.connect(ip, username=credentials[0], password=credentials[1], timeout=10)
    return client


def run(client, command: str) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=120)
    output, errors = stdout.read().decode(), stderr.read().decode()
    if stdout.channel.recv_exit_status():
        raise RuntimeError(errors[-1000:] or output[-1000:])
    return output


def hashes(client, project: str) -> dict[str, str]:
    quoted = " ".join(shlex.quote(path) for path in FILES)
    output = run(client, f"cd {shlex.quote(project)} && sha256sum {quoted}")
    return {name: digest for digest, name in (line.split(maxsplit=1) for line in output.splitlines())}


def deploy(source, target, source_root: str, target_root: str) -> int:
    task = f"artifacts/{TASK}"
    excludes = (
        f"{task}/engines", f"{task}/avd-flat", f"{task}/training-check",
        f"{task}/logs", f"{task}/formal", f"{task}/prototype_acceptance.json",
        f"artifacts/{RESIDENT}/instances", f"artifacts/{RESIDENT}/logs",
    )
    archive = (
        f"artifacts/{ENGINE}", f"artifacts/{RESIDENT}", task,
        f"artifacts/render-web/weights/{CHECKPOINT}",
    )
    command = "tar --sparse -C " + shlex.quote(source_root) + " "
    command += " ".join("--exclude=" + shlex.quote(path) for path in excludes)
    command += " -cf - " + " ".join(shlex.quote(path) for path in archive)
    run(target, "mkdir -p " + shlex.quote(target_root + "/artifacts"))
    _, source_stdout, source_stderr = source.exec_command(command, timeout=3600)
    target_stdin, target_stdout, target_stderr = target.exec_command(
        "tar --sparse -C " + shlex.quote(target_root) + " -xf -", timeout=3600
    )
    sent = 0
    while chunk := source_stdout.channel.recv(1024 * 1024):
        target_stdin.channel.sendall(chunk)
        sent += len(chunk)
    target_stdin.channel.shutdown_write()
    source_error = source_stderr.read().decode()
    target_error = target_stderr.read().decode()
    if source_stdout.channel.recv_exit_status() or target_stdout.channel.recv_exit_status():
        raise RuntimeError(f"archive transfer failed: {source_error[-500:]} {target_error[-500:]}")
    return sent


def repair_links(client, root: str) -> None:
    base = f"{root}/artifacts/{ENGINE}"
    links = {
        "engine-data-headless/update": "battle-resources-lite/runtime-update",
        "engine-data/update": "resources/runtime-update",
        "engine-data-battle-resources-headless/update": "battle-resources-headless/runtime-update",
    }
    for link, destination in links.items():
        source = f"{base}/{destination}"
        target = f"{base}/{link}"
        run(client, f"test -e {shlex.quote(source)} && ln -sfn {shlex.quote(source)} {shlex.quote(target)}")


def install_service(client, root: str) -> str:
    source = ROOT.with_name(RESIDENT) / "service.py"
    target = f"{root}/{SERVICE_PATH}"
    with client.open_sftp() as sftp:
        sftp.put(str(source), target)
    return hashlib.sha256(source.read_bytes()).hexdigest()


def install_task_config(client, root: str) -> str:
    source = ROOT / "config.json"
    target = f"{root}/{TASK_CONFIG_PATH}"
    temporary = target + ".deploying"
    with client.open_sftp() as sftp:
        sftp.put(str(source), temporary)
        sftp.posix_rename(temporary, target)
    return hashlib.sha256(source.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", choices=DESTINATIONS)
    parser.add_argument("--inventory", type=Path, default=ROOT.parents[2] / "MACHINES.md")
    args = parser.parse_args()
    credentials = inventory(args.inventory)
    source_root = f"/home/{credentials[SOURCE_HOST][0]}/firstlight_CR"
    target_root = f"/home/{credentials[args.host][0]}/firstlight_CR"
    source = connect(SOURCE_HOST, credentials[SOURCE_HOST])
    target = connect(args.host, credentials[args.host])
    try:
        expected = hashes(source, source_root)
        sent = deploy(source, target, source_root, target_root)
        repair_links(target, target_root)
        expected[SERVICE_PATH] = install_service(target, target_root)
        expected[TASK_CONFIG_PATH] = install_task_config(target, target_root)
        actual = hashes(target, target_root)
        if expected != actual:
            raise ValueError("deployed runtime hashes differ from .37")
        print(f".{args.host}: verified {len(actual)} files, transferred {sent / 2**30:.2f} GiB")
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    main()
