import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko


def main():
    evidence_root = Path(__file__).resolve().parent
    workspace = evidence_root.parents[2]
    project = workspace / "firstlight_CR"
    previous = evidence_root / "native-migration-1792548905478386000"
    remote = json.loads((previous / "launch.json").read_text())["remote"]
    attempt = "migration-diagnostics-" + str(time.time_ns())
    output = evidence_root / attempt
    output.mkdir()
    files = {"tools/" + name: project / "tools" / name for name in
        ("fair_migration_diagnostics.py", "diagnose_fair_final_migration.py")}
    identities = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    checkpoint_sha = "bc8fd51cc2f7b483d39597643e2f4883fcb9a0b0875f19a5d0091f4c0131ca5a"
    launch = dict(attempt=attempt, remote=remote, source_sha256=identities, max_rows=12,
        checkpoint_sha256=checkpoint_sha, engineering_only=True, training_admissible=False)
    (output / "launch.json").write_text(json.dumps(launch, sort_keys=True, indent=2))
    fields = [value.strip() for value in next(line for line in (workspace / "MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    try:
        with client.open_sftp() as sftp:
            for name, path in files.items():
                destination = remote + "/project/" + name
                try:
                    with sftp.open(destination, "rb") as existing:
                        if hashlib.sha256(existing.read()).hexdigest() != identities[name]:
                            raise ValueError("existing remote diagnostic source differs")
                except FileNotFoundError:
                    sftp.put(str(path), destination)
        python = "/home/zhichun/firstlight_CR/.venv/bin/python"
        report_name = attempt + ".json"
        command = [python, "-u", remote + "/project/tools/diagnose_fair_final_migration.py",
            "--data", remote + "/data", "--manifest", remote + "/manifest.json",
            "--source", "/home/zhichun/fair-teacher-r0-vs-m0-20261014/candidate.pt",
            "--checkpoint", remote + "/engineering-migration-update.pt", "--checkpoint-sha256", checkpoint_sha,
            "--output", remote + "/" + report_name, "--device", "cuda:0", "--max-rows", "12", "--engineering-acceptance"]
        script = f"""
import hashlib,json,subprocess,shutil
from pathlib import Path
root=Path({remote!r})
manifest=json.loads((root/'project/source-manifest.json').read_text())
manifest.update({identities!r})
for name,expected in manifest.items():
 if hashlib.sha256((root/'project'/name).read_bytes()).hexdigest()!=expected: raise ValueError('frozen deployed source differs')
print(json.dumps(dict(source_verified=True,disk_free_gib=shutil.disk_usage(root).free/1024**3)),flush=True)
with (root/{attempt + '.log'!r}).open('x') as log:
 result=subprocess.run({command!r},stdout=log,stderr=subprocess.STDOUT,timeout=600)
print(json.dumps(dict(exit_code=result.returncode)),flush=True)
raise SystemExit(result.returncode)
"""
        _, stdout, stderr = client.exec_command(shlex.join([python, "-u", "-c", script]), timeout=660)
        result, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        print(result + errors, flush=True)
        with client.open_sftp() as sftp:
            sftp.get(remote + "/" + attempt + ".log", str(output / "diagnostic.log"))
            if not code:
                sftp.get(remote + "/" + report_name, str(output / "report.json"))
        (output / "completion.json").write_text(json.dumps(dict(exit_code=code, stdout=result, stderr=errors), indent=2))
        print(json.dumps(dict(attempt=attempt, exit_code=code)), flush=True)
        if code:
            raise RuntimeError((output / "diagnostic.log").read_text()[-4000:])
        report = json.loads((output / "report.json").read_text())
        if any(report["source_sha256"].get(name) != value for name, value in identities.items()):
            raise ValueError("downloaded diagnostic source identity differs")
        print((output / "diagnostic.log").read_text(), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
