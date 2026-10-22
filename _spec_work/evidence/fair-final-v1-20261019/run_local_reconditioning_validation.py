import argparse
import hashlib
import json
from pathlib import Path
import shlex
import time

import paramiko

from run_native_probe import BASE, PYTHON, SOURCE, WORKSPACE
from verify_deck_belief import validate_deck_belief


ROOT = Path(__file__).resolve().parent
PORTS = (29660, 29661)
ENGINE_SHA = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-build", type=Path)
    args = parser.parse_args()
    engine_build = None
    engine_manifest = None
    engine_sha = ENGINE_SHA
    if args.engine_build is not None:
        engine_build = args.engine_build.resolve()
        engine_manifest = json.loads((engine_build/"build-manifest.json").read_text())
        if (engine_manifest["schema"] != "fair-final-phase-snapshot-build.v1"
                or engine_manifest["training_admissible"] is not False
                or engine_manifest["scope"] != "single-slot-phase-snapshot-and-local-draw"
                or engine_manifest["builder_sha256"] != hashlib.sha256(
                    (ROOT/"build_phase_snapshot_engine.py").read_bytes()).hexdigest()):
            raise ValueError("isolated engine build identity differs")
        for relative, expected in engine_manifest["artifacts"].items():
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("isolated build artifact escapes root")
            if hashlib.sha256((engine_build/relative).read_bytes()).hexdigest() != expected:
                raise ValueError("isolated build artifact changed: "+relative)
        for relative, expected in engine_manifest["inputs"].items():
            if hashlib.sha256((WORKSPACE/relative).read_bytes()).hexdigest() != expected:
                raise ValueError("isolated native input changed: "+relative)
        engine_sha = engine_manifest["artifacts"]["libcrprobe.so"]
    validate_deck_belief()
    run_id = f"native-local-reconditioning-{time.time_ns()}"
    output = ROOT/run_id
    output.mkdir()
    remote = "/home/zhichun/fair-final-v1-20261019-local-reconditioning/" + run_id
    fields = [field.strip() for field in next(line for line in (WORKSPACE/"MACHINES.md").read_text().splitlines()
        if "| 172.16.48.35 |" in line).split("|")]
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("172.16.48.35", username=fields[3], password=fields[4], timeout=12)
    client.get_transport().set_keepalive(15)
    try:
        preparation = f'''
from pathlib import Path
import hashlib,json,shutil,socket
root=Path({remote!r})
for port in {PORTS!r}:
 with socket.socket() as check:check.bind(('127.0.0.1',port))
available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/home/zhichun').free
probe=Path({BASE+'/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r})
engine_sha=hashlib.sha256(probe.read_bytes()).hexdigest()
if available<32*1024**3 or free<5*1024**3 or engine_sha!={ENGINE_SHA!r}:raise RuntimeError('prior preflight failed')
root.mkdir(parents=True,exist_ok=False);(root/'project').mkdir()
shutil.copytree({BASE+'/project/native_runner'!r},root/'project/native_runner')
(root/'project/native_runner/training/fair_final').mkdir(exist_ok=True);(root/'project/tools').mkdir()
preflight=dict(memory_available=available,free_bytes=free,engine_sha256=engine_sha)
(root/'preflight.json').write_text(json.dumps(preflight,indent=2));print(json.dumps(preflight),flush=True)
'''
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", preparation]), timeout=60)
        print(stdout.read().decode(), flush=True)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        identities = {}
        worker = ROOT/"local_reconditioning_worker.py"
        prior = ROOT/"public-deck-prior-engineering.json"
        from verify_prior_history import validate_prior_history
        previous = validate_prior_history()
        histories = {Path(relative).name:(ROOT/relative).read_bytes() for relative in previous["artifacts"]
            if Path(relative).name in {f"history-{threshold}-{owner}.json" for threshold in (1200,3600) for owner in (0,1)}}
        if len(histories) != 4:
            raise ValueError("missing archived verified histories")
        settings = dict(root=remote, base=BASE, ports=list(PORTS), engine_sha256=engine_sha)
        if engine_build is not None:
            settings["engine_path"] = remote+"/libcrprobe.so"
        with client.open_sftp() as sftp:
            paths = list((SOURCE/"native_runner/training/fair_final").glob("*.py"))
            paths.append(SOURCE/"native_runner/training_capture.py")
            paths.extend(SOURCE/"tools"/name for name in ("probe_fair_final_local_reconditioning.py", "probe_fair_final_visibility.py"))
            for path in paths:
                relative, content = str(path.relative_to(SOURCE)), path.read_bytes()
                identities[relative] = hashlib.sha256(content).hexdigest()
                with sftp.open(remote+"/project/"+relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote+"/project/"+relative, "rb") as handle:
                    if hashlib.sha256(handle.read()).hexdigest() != identities[relative]:
                        raise ValueError("prior deployed source differs")
            uploads = {**histories, "project/run.py":worker.read_bytes(), prior.name:prior.read_bytes(),
                "project/probe-source-manifest.json":json.dumps(identities, sort_keys=True).encode(),
                "run-manifest.json":json.dumps(settings).encode()}
            if engine_build is not None:
                uploads.update({"libcrprobe.so":(engine_build/"libcrprobe.so").read_bytes(),
                    "project/phase_snapshot_launcher.py":(ROOT/"phase_snapshot_launcher.py").read_bytes(),
                    "engine-build-manifest.json":(engine_build/"build-manifest.json").read_bytes()})
            for relative, content in uploads.items():
                with sftp.open(remote+"/"+relative, "wb") as handle:
                    handle.write(content)
                with sftp.open(remote+"/"+relative, "rb") as handle:
                    if handle.read() != content:
                        raise ValueError("prior deployment payload differs")
        verification = f'''
from pathlib import Path
import hashlib,json
root=Path({remote!r})
settings=json.loads((root/'run-manifest.json').read_text())
probe=Path(settings.get('engine_path',{BASE+'/project/research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so'!r}))
digest=hashlib.sha256(probe.read_bytes()).hexdigest()
if digest!=settings['engine_sha256']:raise ValueError('deployed engine identity differs')
preflight=json.loads((root/'preflight.json').read_text())
preflight['baseline_engine_sha256']=preflight['engine_sha256']
preflight['engine_sha256']=digest
(root/'preflight.json').write_text(json.dumps(preflight,indent=2))
'''
        _, stdout, stderr = client.exec_command(shlex.join([PYTHON, "-c", verification]), timeout=30)
        if stdout.channel.recv_exit_status():
            raise RuntimeError(stderr.read().decode())
        attempt = dict(remote=remote, ports=list(PORTS), source_sha256=identities,
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest(),
            prior_file_sha=hashlib.sha256(prior.read_bytes()).hexdigest())
        (output/"attempt.json").write_text(json.dumps(attempt, indent=2))
        print(json.dumps(dict(status="running", remote=remote, output=str(output), ports=PORTS)), flush=True)
        command = "cd "+shlex.quote(remote+"/project")+" && "+shlex.join([PYTHON, "run.py", remote+"/run-manifest.json"])
        _, stdout, stderr = client.exec_command(command, timeout=1900)
        collected, errors = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        (output/"attempt.json").write_text(json.dumps(dict(attempt, exit_code=code, stdout=collected, stderr=errors), indent=2))
        with client.open_sftp() as sftp:
            for name in sftp.listdir(remote):
                if name.endswith((".json", ".log")):
                    sftp.get(remote+"/"+name, str(output/name))
            (output/"project").mkdir()
            for name in ("run.py", "probe-source-manifest.json", *(["phase_snapshot_launcher.py"] if engine_build else [])):
                sftp.get(remote+"/project/"+name, str(output/"project"/name))
            for port in PORTS:
                directory = f"engine-{port}"
                try:
                    names = sftp.listdir(remote+"/"+directory)
                except FileNotFoundError:
                    continue
                (output/directory).mkdir(exist_ok=True)
                for name in names:
                    if name.endswith((".json", ".log")):
                        sftp.get(remote+"/"+directory+"/"+name, str(output/directory/name))
        if (output/"probe.log").exists():
            print((output/"probe.log").read_text()[-8000:], flush=True)
        if code:
            print((output/"cleanup.json").read_text(), flush=True)
            print(errors, flush=True)
            raise SystemExit(code)
        cleanup = json.loads((output/"cleanup.json").read_text())
        if not cleanup["clean"] or cleanup["error"] is not None:
            raise ValueError("prior integration cleanup incomplete")
        report = dict(schema="fair-final-local-reconditioning-suite.v1", status="passed", training_admissible=False,
            **attempt, report=json.loads((output/"report.json").read_text()), cleanup=cleanup,
            artifacts={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.rglob("*") if path.is_file()})
        (ROOT/"native-local-reconditioning-report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(dict(status="passed", roots=len(report["report"]["records"]),
            seconds=report["report"]["seconds"])), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
