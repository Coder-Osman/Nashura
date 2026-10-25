import argparse
import json
from pathlib import Path
import shutil
import sys
import time


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "fair-final-random-execution-v1"))
from run_trace import connect, execute, PYTHON
from verify_parent import verify
from run_selection import GATES, pinned, publish, read, sha
from selection_ssh import local_private_root


def prepare(parent, *, launch_sha, receipt_sha, data_roots, private_root, timeout_seconds=86400):
    if type(timeout_seconds) is not int or not 1800 <= timeout_seconds <= 86400:
        raise ValueError("independent bounded retry budget differs")
    verification = verify(parent, launch_sha=launch_sha, receipt_sha=receipt_sha, data_roots=data_roots)
    parent = Path(parent).resolve()
    launch = read(parent / "launch.json")
    receipt = read(parent / "native-download-receipt.json")
    attempt = local_private_root(private_root) / ("selection-retry-" + str(time.time_ns()))
    attempt.mkdir(mode=0o700)
    remote = str(Path(launch["remote_base"]) / attempt.name)
    uploads = {"retry_selection.py": ROOT / "retry_selection.py",
        "run_selection.py": parent / "run_selection.py",
        "selection_remote_runtime.py": parent / "selection_remote_runtime.py",
        "parent-remote-config.json": parent / "remote-config.json"}
    for name, source in uploads.items():
        (attempt / name).write_bytes(source.read_bytes())
    request = dict(schema="FairFinalSelectionTimeoutRetry/v1", parent_remote=launch["remote_root"] + "/selection",
        remote=remote, verification=verification, timeout_seconds=timeout_seconds,
        parent_processes=[receipt["remote_inventory"]["remote_process"]["process"],
            read(parent / "selection/status.json")["process"]],
        files=receipt["remote_inventory"]["files"],
        runtime_sha256={name: sha(attempt / name) for name in uploads}, **GATES)
    publish(attempt / "request.json", request)
    publish(attempt / "preparation.json", dict(status="prepared_not_launched",
        parent_local=str(parent), request_sha256=sha(attempt / "request.json"),
        runner_sha256=sha(__file__), verifier_sha256=sha(ROOT / "verify_parent.py"),
        terminal_verifier_sha256=sha(ROOT / "verify_retry.py"), **GATES))
    return dict(attempt=str(attempt), preparation_sha256=sha(attempt / "preparation.json"), **GATES)


def launch(attempt, *, preparation_sha):
    attempt = Path(attempt).resolve()
    pinned(attempt / "preparation.json", preparation_sha)
    prepared = read(attempt / "preparation.json")
    if (prepared["status"] != "prepared_not_launched" or prepared["runner_sha256"] != sha(__file__)
            or prepared["verifier_sha256"] != sha(ROOT / "verify_parent.py")
            or prepared["terminal_verifier_sha256"] != sha(ROOT / "verify_retry.py")
            or any((attempt / name).exists() for name in ("launch.json", "launch-intent.json"))):
        raise ValueError("retry preparation differs or was already launched")
    pinned(attempt / "request.json", prepared["request_sha256"])
    request = read(attempt / "request.json")
    from retry_selection import validate
    validate(request)
    for name, expected in request["runtime_sha256"].items():
        pinned(attempt / name, expected)
    config = read(attempt / "parent-remote-config.json")
    if config["python"] != PYTHON or config["gpu_index"] != 1:
        raise ValueError("retry deployment must use the pinned engineering device")
    remote = request["remote"]
    publish(attempt / "launch-intent.json", dict(remote=remote,
        request_sha256=prepared["request_sha256"], status="inspect_remote_before_any_retry", **GATES))
    client = connect()
    try:
        execute(client, f"from pathlib import Path; Path({remote!r}).mkdir(mode=0o700, exist_ok=False)")
        with client.open_sftp() as sftp:
            for name in (*request["runtime_sha256"], "request.json"):
                sftp.put(str(attempt / name), remote + "/" + name)
        process = json.loads(execute(client, f'''
import hashlib,json,subprocess
from pathlib import Path
root=Path({remote!r})
for name,expected in {dict(request['runtime_sha256'], **{'request.json': prepared['request_sha256']})!r}.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise ValueError('retry upload differs')
with (root/'worker.log').open('x') as log:
 child=subprocess.Popen([{PYTHON!r},'-B','-u',str(root/'retry_selection.py'),str(root),'--request-sha',{prepared['request_sha256']!r}],cwd=root,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
process=dict(pid=child.pid,start_ticks=Path('/proc',str(child.pid),'stat').read_text().rsplit(')',1)[1].split()[19])
(root/'worker-process.json').write_text(json.dumps(process))
print(json.dumps(process))
'''))
        publish(attempt / "launch.json", dict(schema="FairFinalSelectionTimeoutLaunch/v1", remote=remote,
            preparation_sha256=preparation_sha, request_sha256=prepared["request_sha256"], process=process, **GATES))
        return dict(status="launched_requires_inspection", attempt=str(attempt), process=process,
            launch_sha256=sha(attempt / "launch.json"), **GATES)
    finally:
        client.close()


def collect(attempt, *, launch_sha):
    attempt = Path(attempt).resolve()
    pinned(attempt / "launch.json", launch_sha)
    record = read(attempt / "launch.json")
    pinned(attempt / "preparation.json", record["preparation_sha256"])
    prepared = read(attempt / "preparation.json")
    pinned(attempt / "request.json", record["request_sha256"])
    request = read(attempt / "request.json")
    client = connect()
    try:
        state = json.loads(execute(client, f'''
import json,sys
sys.path.insert(0,{record['remote']!r})
from retry_selection import inventory
print(json.dumps(inventory({record['remote']!r},{record['process']!r},{record['request_sha256']!r})))
'''))
        if state["running"]:
            publish(attempt / ("poll-" + str(time.time_ns()) + ".json"), state)
            return dict(status=state["state"], running=state["running"], **GATES)
        archive = attempt / "archive"
        archive.mkdir(exist_ok=True)
        parent = Path(prepared["parent_local"]) / "selection"
        with client.open_sftp() as sftp:
            for name, metadata in state["files"].items():
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
                    raise ValueError("retry archive path differs")
                destination = archive / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = parent / relative.relative_to("selection") if relative.parts[0] == "selection" else attempt / relative
                if destination.exists():
                    pinned(destination, metadata["sha256"])
                elif source.is_file() and sha(source) == metadata["sha256"]:
                    shutil.copyfile(source, destination)
                else:
                    temporary = destination.with_name(destination.name + ".partial")
                    sftp.get(record["remote"] + "/" + name, str(temporary))
                    pinned(temporary, metadata["sha256"])
                    temporary.replace(destination)
                if destination.stat().st_size != metadata["bytes"]:
                    raise ValueError("retry archive byte count differs")
        after = json.loads(execute(client, f'''
import json,sys
sys.path.insert(0,{record['remote']!r})
from retry_selection import inventory
print(json.dumps(inventory({record['remote']!r},{record['process']!r},{record['request_sha256']!r})))
'''))
        if after["running"] or after["files"] != state["files"] or after["state"] != state["state"]:
            raise ValueError("retry archive changed during download")
        receipt = dict(schema="FairFinalSelectionTimeoutReceipt/v1", launch_sha256=launch_sha,
            inventory=after, status=after["state"], **GATES)
        if (attempt / "receipt.json").exists():
            previous = read(attempt / "receipt.json")
            if (previous["launch_sha256"] != launch_sha or previous["status"] != receipt["status"]
                    or any(previous["inventory"][name] != after[name] for name in
                        ("files", "process", "request_sha256", "running", "lease_available"))):
                raise ValueError("retry archive differs from preserved receipt")
        else:
            publish(attempt / "receipt.json", receipt)
        return dict(status=after["state"], receipt_sha256=sha(attempt / "receipt.json"), **GATES)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("parent", type=Path)
    prepare_parser.add_argument("--launch-sha", required=True)
    prepare_parser.add_argument("--receipt-sha", required=True)
    prepare_parser.add_argument("--data-roots", type=Path, nargs="+", required=True)
    prepare_parser.add_argument("--private-root", type=Path, required=True)
    prepare_parser.add_argument("--timeout-seconds", type=int, default=86400)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("attempt", type=Path)
    launch_parser.add_argument("--preparation-sha", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("attempt", type=Path)
    collect_parser.add_argument("--launch-sha", required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    result = {"prepare": prepare, "launch": launch, "collect": collect}[command](**args)
    print(json.dumps(result), flush=True)
