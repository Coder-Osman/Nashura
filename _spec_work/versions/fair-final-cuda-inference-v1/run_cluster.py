from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import shlex
import sys
import tarfile
import time

import paramiko
import torch

from probe import compare, sha


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
SOURCE = WORKSPACE / "firstlight_CR"
PARENT = ROOT.parent / "fair-final-search-continuation-v1/native-search-continuation-1792579051065329000"
CHECKPOINT = ROOT.parent / "fair-final-migration-series-v1/native-source-a.pt"
SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"


def remote_call(client, command, *, timeout=30):
    _, stdout, stderr = client.exec_command(shlex.join(command), timeout=timeout)
    output, errors = stdout.read().decode(), stderr.read().decode()
    status = stdout.channel.recv_exit_status()
    return dict(exit_code=status, stdout=output, stderr=errors)


def main():
    sys.path.insert(0, str(ROOT.parent / "fair-final-search-continuation-v1"))
    from continuation_evidence import audit_checkpoint_prefix
    from tools.probe_fair_final_search_episode import SearchTrace

    receipt = audit_checkpoint_prefix(PARENT)
    if sha(CHECKPOINT) != SOURCE_SHA:
        raise ValueError("local source A differs")
    attempt = "project-cuda-" + str(time.time_ns())
    output = ROOT / attempt
    output.mkdir()
    trace = SearchTrace(PARENT / "evidence/episode/search-boundaries")
    ticks = sorted(int(name.removesuffix(".pt.gz")) for name in receipt["boundary_sha256"])
    cases = []
    for tick in (ticks[0], ticks[len(ticks) // 2], ticks[-1]):
        boundary = trace.load(tick)
        cases.extend(dict(tick=tick, owner=int(owner), information=information)
            for owner, information in sorted(boundary["information"].items()))
    buffer = io.BytesIO()
    torch.save(cases, buffer)
    files = {"frames.pt": buffer.getvalue(), "probe.py": (ROOT / "probe.py").read_bytes()}
    for relative, expected in receipt["source_sha256"].items():
        path = SOURCE / relative
        if sha(path) != expected:
            raise ValueError("frozen source changed")
        files["project/" + relative] = path.read_bytes()
    for path in sorted((SOURCE / "native_runner/data").rglob("*")):
        if path.is_file():
            files["project/" + str(path.relative_to(SOURCE))] = path.read_bytes()
    identity = dict(schema="FairFinalCudaInferenceInputs/v1", checkpoint_sha256=SOURCE_SHA,
        training_admissible=False, gates_closed=False, parent_receipt_sha=sha(PARENT / "download-receipt.json"),
        ticks=[case["tick"] for case in cases], files={name: hashlib.sha256(data).hexdigest() for name, data in files.items()})
    files["identity.json"] = json.dumps(identity, sort_keys=True).encode()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    package = payload.getvalue()
    (output / "payload.tar.gz").write_bytes(package)
    (output / "identity.json").write_bytes(files["identity.json"])
    machines = (WORKSPACE / "MACHINES.md").read_text().splitlines()

    def run(number):
        host = "172.16.48." + str(number)
        destination = output / str(number)
        destination.mkdir()
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        result = dict(host=host, training_admissible=False, gates_closed=False)
        try:
            fields = [part.strip() for part in next(line for line in machines if "| " + host + " |" in line).split("|")]
            client.connect(host, username=fields[3], password=fields[4], timeout=12)
            remote = "/dev/shm/" + attempt
            preparation = f'''
import hashlib,json,shutil
from pathlib import Path
home=Path.home()
memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))/1024**2
if memory<32 or shutil.disk_usage('/dev/shm').free<512*1024**2:raise ValueError('bounded inference resource floor failed')
python=home/'firstlight_CR/.venv/bin/python'
if not python.is_file():raise ValueError('known CR runtime missing')
root=Path({remote!r})
root.mkdir(exist_ok=False)
checkpoint=home/'fair-teacher-r0-vs-m0-20261014/candidate.pt'
reuse=checkpoint.is_file() and hashlib.sha256(checkpoint.read_bytes()).hexdigest()=={SOURCE_SHA!r}
print(json.dumps(dict(python=str(python),checkpoint=str(checkpoint) if reuse else str(root/'source-a.pt'),upload_checkpoint=not reuse,available_gib=memory)))
'''
            prepared = remote_call(client, ["python3", "-c", preparation])
            if prepared["exit_code"]:
                raise RuntimeError(prepared["stderr"])
            settings = json.loads(prepared["stdout"])
            result.update(remote=remote, settings=settings, payload_sha256=hashlib.sha256(package).hexdigest())
            with client.open_sftp() as sftp:
                sftp.get_channel().settimeout(90)
                with sftp.open(remote + "/payload.tar.gz", "wb") as handle:
                    handle.write(package)
                if settings["upload_checkpoint"]:
                    sftp.put(str(CHECKPOINT), settings["checkpoint"])
            unpack = f'''
import hashlib,tarfile
from pathlib import Path
root=Path({remote!r})
if hashlib.sha256((root/'payload.tar.gz').read_bytes()).hexdigest()!={result['payload_sha256']!r}:raise ValueError('payload transfer differs')
with tarfile.open(root/'payload.tar.gz') as archive:archive.extractall(root,filter='data')
'''
            extracted = remote_call(client, [settings["python"], "-B", "-c", unpack])
            if extracted["exit_code"]:
                raise RuntimeError(extracted["stderr"])
            result["execution"] = remote_call(client, ["timeout", "180s", settings["python"], "-B",
                remote + "/probe.py", "--root", remote, "--checkpoint", settings["checkpoint"]], timeout=195)
            if result["execution"]["exit_code"]:
                result["status"] = "failed"
            else:
                with client.open_sftp() as sftp:
                    sftp.get(remote + "/result.json", str(destination / "result.json"))
                report = json.loads((destination / "result.json").read_text())
                if (report["identity_sha256"] != hashlib.sha256(files["identity.json"]).hexdigest()
                        or report["checkpoint_sha256"] != SOURCE_SHA):
                    raise ValueError("returned inference identity differs")
                result.update(status=report["status"], errors=report["errors"],
                    result_sha256=sha(destination / "result.json"))
        except Exception as error:
            result.update(status="failed", error=repr(error))
        finally:
            client.close()
            (destination / "execution.json").write_text(json.dumps(result, indent=2))
        print(json.dumps({key: value for key, value in result.items() if key not in ("execution", "settings")}), flush=True)
        return result

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(run, (34, 35, 37, 39, 41)))
    report = dict(schema="FairFinalClusterProjectInference/v1", results=results,
        training_admissible=False, gates_closed=False, cross_host_errors={}, status="failed")
    if all(result["status"] == "passed_limited_scope" for result in results):
        reference = json.loads((output / "35/result.json").read_text())
        for number in (34, 35, 37, 39, 41):
            actual = json.loads((output / str(number) / "result.json").read_text())
            if actual["network_sha"] != reference["network_sha"]:
                raise ValueError("cross-host network identity differs")
            report["cross_host_errors"][str(number)] = compare(reference["gpu"], actual["gpu"])
        report["status"] = "passed_limited_scope"
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(dict(output=str(output), status=report["status"], cross_host_errors=report["cross_host_errors"])), flush=True)


if __name__ == "__main__":
    main()
