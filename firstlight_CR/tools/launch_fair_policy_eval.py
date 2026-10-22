"""Deploy and launch the frozen step-24 versus step-0 policy evaluation."""

import hashlib
import os
from pathlib import Path
import shlex

import paramiko


HOSTS = ("35", "37", "39", "41")
ROOT = "/home/zhichun/fair-eval-20261011-step24"
PROJECT = "/home/zhichun/fair-g1-20261010/project"
POLICY = f"{PROJECT}/native_runner/training/fair_search"
RUNTIME = "/home/zhichun/firstlight_CR/.venv/bin/python"
CANDIDATE_SHA = "4c4b68877d6fa25f8c0ae692e177b8bd1fb81238b64f4823cb002f6c47ac6627"
BASELINE_SHA = "26d592cd58876eceb60c9f34a5ce02f18df24d003991d74e4fdd1af90a5f1ee7"
LOCAL = Path(__file__).resolve().parents[1]
FILES = (
    (LOCAL / "native_runner/training/fair_search/evaluate_policy.py", f"{POLICY}/evaluate_policy.py"),
    (Path("/tmp/fair-eval-candidate-policy-step24.pt"), f"{POLICY}/fair-eval-candidate-policy-step24.pt"),
    (Path("/tmp/fair-eval-baseline-policy-step0.pt"), f"{POLICY}/fair-eval-baseline-policy.pt"),
)


def remote(client, command):
    _, stdout, stderr = client.exec_command(command, timeout=30)
    result = stdout.read().decode().strip()
    error = stderr.read().decode().strip()
    if stdout.channel.recv_exit_status():
        raise RuntimeError(f"remote command failed: {command}\n{error}")
    return result


def main():
    password = os.environ["FAIR_EVAL_SSH_PASSWORD"]
    for host_index, host in enumerate(HOSTS):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(f"172.16.48.{host}", username="zhichun", password=password, timeout=10)
        try:
            remote(client, f"mkdir -p {shlex.quote(ROOT)}")
            sftp = client.open_sftp()
            try:
                for source, destination in FILES:
                    local_sha = hashlib.sha256(source.read_bytes()).hexdigest()
                    current = remote(client, f"sha256sum {shlex.quote(destination)} 2>/dev/null || true")
                    if not current.startswith(local_sha):
                        sftp.put(str(source), destination)
                    actual = remote(client, f"sha256sum {shlex.quote(destination)}").split()[0]
                    if actual != local_sha:
                        raise ValueError(f"{host}: deployed file differs: {destination}")
            finally:
                sftp.close()
            for slot in range(8):
                shard = host_index * 8 + slot
                start = shard * 1000 // 32
                end = (shard + 1) * 1000 // 32
                output = f"{ROOT}/shard-{shard:02d}"
                command = [RUNTIME, "-u", "-m", "native_runner.training.fair_search.evaluate_policy",
                           "--candidate", FILES[1][1], "--baseline", FILES[2][1],
                           "--expected-candidate-sha256", CANDIDATE_SHA,
                           "--expected-baseline-sha256", BASELINE_SHA,
                           "--puct-root", f"{PROJECT}/research/puct-optimized",
                           "--project", PROJECT,
                           "--engine-script", "/home/zhichun/fair-g1-20261010/isolated_engine.py",
                           "--output", output, "--port", str(31200 + slot),
                           "--start-pair", str(start), "--pairs", str(end - start),
                           "--seed-base", "440000", "--deadline", "600", "--device", f"cuda:{slot}"]
                quoted = shlex.join(command)
                launch = (f"mkdir -p {shlex.quote(output)}; "
                          f"if [ -s {shlex.quote(output)}/run.pid ] && "
                          f"kill -0 $(cat {shlex.quote(output)}/run.pid) 2>/dev/null; then "
                          f"cat {shlex.quote(output)}/run.pid; else "
                          f"cd {shlex.quote(PROJECT)}; "
                          f"nohup {quoted} > {shlex.quote(output)}/run.log 2>&1 < /dev/null & "
                          f"echo $! | tee {shlex.quote(output)}/run.pid; fi")
                pid = remote(client, launch)
                print(f"{host} shard={shard:02d} pairs={start}:{end} pid={pid}", flush=True)
        finally:
            client.close()


if __name__ == "__main__":
    main()
