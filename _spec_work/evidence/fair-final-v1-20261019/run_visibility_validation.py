import argparse
import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE


ROOT = Path(__file__).resolve().parent
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"


def collect_result(client, remote, output, case):
    with client.open_sftp() as sftp:
        for name in ("probe.log", "cleanup.json", "report.json", "engine/process.json", "episode.tar.gz",
                "project/probe-source-manifest.json", "project/run.py"):
            try:
                sftp.get(remote + "/" + name, str(output / Path(name).name))
            except FileNotFoundError:
                pass
    cleanup = json.loads((output / "cleanup.json").read_text())
    if (cleanup["case"] != case or cleanup["exit_code"] != 0 or cleanup["phase"] != "stopped"
            or cleanup["listening"] or any(cleanup["pids_alive"].values())):
        raise ValueError("remote probe failed or cleanup is incomplete")
    manifest = json.loads((output / "probe-source-manifest.json").read_text())
    if not manifest:
        raise ValueError("missing deployed source identity")
    for relative, expected in manifest.items():
        if hashlib.sha256((SOURCE / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"collected probe source is stale: {relative}")
    if case == "timing":
        (ROOT / "native-timing-report.json").write_bytes((output / "report.json").read_bytes())
        (ROOT / "native-probe-process.json").write_bytes((output / "process.json").read_bytes())
    elif case in ("episode", "draw"):
        match_id = "p1-native-draw" if case == "draw" else "p1-complete-native-current"
        prefix = f"episode-attempt-{match_id}-1-{time.time_ns()}"
        (ROOT / (prefix + ".tar.gz")).write_bytes((output / "episode.tar.gz").read_bytes())
        (ROOT / (prefix + "-process.json")).write_bytes((output / "process.json").read_bytes())
    else:
        report = json.loads((output / "report.json").read_text())
        if report["source_sha256"] != manifest:
            raise ValueError("report differs from deployed manifest")
        report["cleanup"] = cleanup
        report["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        report["artifacts"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in output.iterdir() if path.is_file()}
        (ROOT / "trajectory-visibility-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(status="passed", case=case, output=str(output))), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("visibility", "timing", "episode", "draw"), required=True)
    parser.add_argument("--collect-existing")
    args = parser.parse_args()
    run_id = args.collect_existing or f"visibility-{args.case}-{time.time_ns()}"
    if (Path(run_id).name != run_id or not run_id.startswith(f"visibility-{args.case}-")
            or not run_id.removeprefix(f"visibility-{args.case}-").isdigit()):
        raise ValueError("invalid isolated run identity")
    remote = "/home/zhichun/fair-final-v1-20261019-visibility/" + run_id
    port = {"visibility": 29622, "timing": 29619, "episode": 29620, "draw": 29623}[args.case]
    output = ROOT / run_id
    output.mkdir(exist_ok=bool(args.collect_existing))
    row = next(line for line in (WORKSPACE / "MACHINES.md").read_text().splitlines() if "| 172.16.48.35 |" in line)
    fields = [field.strip() for field in row.split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    client.get_transport().set_keepalive(15)
    try:
        if args.collect_existing:
            collect_result(client, remote, output, args.case)
            return
        preparation = f"""
from pathlib import Path
import shutil,socket
root=Path({remote!r})
root.mkdir(parents=True,exist_ok=False)
with socket.socket() as check: check.bind(('127.0.0.1',{port}))
if shutil.disk_usage(root).free<5*1024**3: raise RuntimeError('probe disk threshold')
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
if available<32*1024**3: raise RuntimeError('probe memory threshold')
(root/'project').mkdir()
shutil.copytree({BASE + '/project/native_runner'!r},root/'project/native_runner')
(root/'project/native_runner/training/fair_final').mkdir(exist_ok=True)
(root/'project/tools').mkdir()
print({{'memory_available':available,'free_bytes':shutil.disk_usage(root).free}},flush=True)
"""
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=60)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        print(stdout.read().decode(), flush=True)
        scripts = {"visibility": "probe_fair_final_visibility.py", "timing": "probe_fair_final_timing.py",
            "episode": "probe_fair_final_episode.py", "draw": "probe_fair_final_episode.py"}
        source_identity = {}
        match_id = "p1-native-draw" if args.case == "draw" else "p1-complete-native-current"
        command = [PYTHON, remote + "/project/tools/" + scripts[args.case], "--port", str(port),
            "--source", BASE + "/candidate.pt"]
        if args.case in ("visibility", "timing"):
            command += ["--output", remote + "/report.json", "--puct-root", BASE + "/project/research/puct-optimized"]
        else:
            command += ["--output", remote + "/data", "--engine-sha256", ENGINE_SHA,
                "--match-id", match_id, "--attempt-id", "1", "--engine-seed",
                "78101907" if args.case == "draw" else "78101906", "--execution-seed", "78101946",
                "--wall-seconds", "900"]
            if args.case == "draw":
                command.append("--wait-only")
        with client.open_sftp() as sftp:
            paths = list((SOURCE / "native_runner/training/fair_final").glob("*.py"))
            paths.extend(SOURCE / "tools" / name for name in sorted(set(scripts.values())))
            for path in paths:
                relative = str(path.relative_to(SOURCE))
                content = path.read_bytes()
                source_identity[relative] = hashlib.sha256(content).hexdigest()
                target = remote + "/project/" + relative
                with sftp.open(target, "wb") as handle:
                    handle.write(content)
                with sftp.open(target, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != source_identity[relative]:
                        raise ValueError("deployed probe source differs")
            with sftp.open(remote + "/project/probe-source-manifest.json", "w") as handle:
                handle.write(json.dumps(source_identity, sort_keys=True))
            runner = f"""
import hashlib,json,os,socket,subprocess,sys,tarfile,time
from pathlib import Path
root=Path({remote!r})
probe=Path({BASE + '/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r})
if hashlib.sha256(probe.read_bytes()).hexdigest()!={ENGINE_SHA!r}: raise ValueError('engine SHA differs')
environment=dict(os.environ,CR_PROBE=str(probe),PYTHONUNBUFFERED='1')
from native_runner.training.fair_search.evaluate_policy import wait_for_engine
child=None
with (root/'engine.log').open('a') as log:
 engine=subprocess.Popen([sys.executable,{BASE + '/isolated_engine.py'!r},'--project',{BASE + '/project'!r},'--port',{str(port)!r},'--instance',str(root/'engine')],stdout=log,stderr=subprocess.STDOUT)
 try:
  wait_for_engine(engine,root/'engine',timeout=120)
  with (root/'probe.log').open('a') as output:
   child=subprocess.Popen({command!r},stdout=output,stderr=subprocess.STDOUT,env=environment)
   started=time.monotonic()
   while child.poll() is None:
    if time.monotonic()-started>1260: raise TimeoutError('bounded probe exceeded wall limit')
    time.sleep(10)
   code=child.returncode
 finally:
  if child is not None and child.poll() is None:
   child.terminate()
   try: child.wait(timeout=10)
   except subprocess.TimeoutExpired: child.kill(); child.wait()
  engine.terminate()
  try: engine.wait(timeout=25)
  except subprocess.TimeoutExpired: engine.kill(); engine.wait()
process=json.loads((root/'engine/process.json').read_text())
alive={{key: process.get(key) is not None and Path('/proc/'+str(process[key])).exists() for key in ('pid','child_pid')}}
with socket.socket() as check: listening=check.connect_ex(('127.0.0.1',{port}))==0
cleanup=dict(case={args.case!r},port={port},phase=process['phase'],pids_alive=alive,listening=listening,exit_code=code)
(root/'cleanup.json').write_text(json.dumps(cleanup,indent=2))
print(json.dumps(cleanup),flush=True)
attempt=root/'data/attempts'/{match_id!r}/'1'
if attempt.exists():
 with tarfile.open(root/'episode.tar.gz','w:gz') as archive:
  archive.add(attempt,arcname='attempt')
  audit=root/'data/audit'/{match_id!r}/'1'
  if audit.exists(): archive.add(audit,arcname='audit')
sys.exit(code if code else int(listening or any(alive.values()) or process['phase']!='stopped'))
"""
            with sftp.open(remote + "/project/run.py", "w") as handle:
                handle.write(runner)
        print(json.dumps(dict(status="running", case=args.case, remote=remote, port=port)), flush=True)
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, remote + "/project/run.py"]), timeout=1450)
        log, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        print(log, flush=True)
        print(errors, flush=True)
        (output / "attempt.json").write_text(json.dumps(dict(remote=remote, exit_code=code,
            source_sha256=source_identity, stdout=log, stderr=errors), indent=2))
        if code:
            with client.open_sftp() as sftp:
                sftp.get(remote + "/probe.log", str(output / "probe.log"))
            print((output / "probe.log").read_text()[-16000:], flush=True)
            raise SystemExit(code)
        collect_result(client, remote, output, args.case)
    finally:
        client.close()


if __name__ == "__main__":
    main()
