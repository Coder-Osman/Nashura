"""Serial Mac collection -> eight-epoch remote training -> fresh data, with receipts."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import traceback

from native_runner.training.sspi.tower_shared import source_sha, MAX_EPOCHS, RUN_SCHEMA
from native_runner.training.sspi.value_experiment import _atomic_json


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ssh_python(args, code, payload):
    command=shlex.join([args.remote_python,"-c",code,json.dumps(payload)])
    result=subprocess.run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=15",args.remote,command],
        check=True,capture_output=True,text=True)
    return json.loads(result.stdout)


def alive(pid, token):
    result=subprocess.run(["ps","-p",str(pid),"-o","command="],capture_output=True,text=True)
    argv=shlex.split(result.stdout)
    return result.returncode==0 and token in argv and "native_runner.training.sspi.tower_refresh" in argv


def collect_batch(args, batch, run_id, excluded):
    batch.mkdir(parents=True,exist_ok=True)
    result=batch/"result.json"
    if result.exists():
        return json.loads(result.read_text())
    launch=batch/"launch.json"
    if launch.exists():
        record=json.loads(launch.read_text())
        if not alive(record["pid"],run_id):
            raise RuntimeError("previous collector exited without a complete batch; inspect its status and log")
    else:
        command=[sys.executable,"-u","-m","native_runner.training.sspi.tower_refresh",
            "--run-dir",str(batch),"--run-id",run_id,"--initial-model",str(args.initial_model),
            "--excluded-seeds",str(excluded),"--actual-port",str(args.actual_port),
            "--belief-port",str(args.belief_port),"--belief-port-owner-1",str(args.belief_port_owner_1)]
        with (batch/"collector.log").open("xb") as log:
            process=subprocess.Popen(command,cwd=args.frozen_source,stdout=log,stderr=subprocess.STDOUT,
                start_new_session=True)
        record=dict(pid=process.pid,command=command)
        _atomic_json(launch,record)
    while not result.exists():
        if not alive(record["pid"],run_id):
            raise RuntimeError("fresh collector exited; incomplete games are not training data")
        time.sleep(15)
    report=json.loads(result.read_text())
    if report.get("status")!="complete" or report.get("run_id")!=run_id:
        raise ValueError("fresh collection receipt differs")
    return report


LAUNCH_REMOTE = r'''
import json,os,pathlib,subprocess,sys,hashlib
p=json.loads(sys.argv[1]);root=pathlib.Path(p["root"]);run=root/"run"
root.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,p["source"])
from native_runner.training.sspi.tower_shared import source_sha
assert source_sha()==p["source_sha"],"remote source differs"
assert hashlib.sha256((root/"data/dataset.json").read_bytes()).hexdigest()==p["dataset_sha"]
launch=root/"launch.json"
if launch.exists():
 print(launch.read_text());raise SystemExit
command=[p["python"],"-u","-m","native_runner.training.sspi.tower_shared","run",
 "--data-dir",str(root/"data"),"--run-dir",str(run),"--run-id",p["run_id"],
 "--device","cuda:0","--parent-checkpoint",p["parent"],"--parent-sha",p["parent_sha"]]
env=dict(os.environ,PYTHONPATH=p["source"],CUDA_VISIBLE_DEVICES="0",CUBLAS_WORKSPACE_CONFIG=":4096:8")
with (root/"trainer.log").open("xb") as log:
 process=subprocess.Popen(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
record=dict(pid=process.pid,command=command,dataset_sha=p["dataset_sha"],source_sha=p["source_sha"])
launch.write_text(json.dumps(record)+"\n")
print(json.dumps(record))
'''


READ_REMOTE = r'''
import json,pathlib,sys,hashlib
p=json.loads(sys.argv[1]);root=pathlib.Path(p["root"]);run=root/"run"
launch=json.loads((root/"launch.json").read_text())
proc=pathlib.Path("/proc")/str(launch["pid"])/"cmdline"
argv=proc.read_bytes().split(b"\0") if proc.exists() else []
result=dict(alive=p["run_id"].encode() in argv and b"native_runner.training.sspi.tower_shared" in argv,
 status=None,report=None)
if (run/"status.json").exists():result["status"]=json.loads((run/"status.json").read_text())
if (run/"result.json").exists():
 report=json.loads((run/"result.json").read_text())
 assert report["identity"]["run_id"]==p["run_id"]
 assert report["identity"]["dataset_sha"]==p["dataset_sha"]
 assert report["checkpoint_sha"]==hashlib.sha256((run/"best.pt").read_bytes()).hexdigest()
 result["report"]=report
print(json.dumps(result))
'''


def validate_training_receipt(report, dataset_sha):
    if (report.get("schema")!=RUN_SCHEMA or report.get("epochs")!=MAX_EPOCHS
            or report["identity"].get("max_epochs")!=8
            or report["identity"].get("dataset_sha")!=dataset_sha
            or report["identity"].get("source_sha")!=source_sha()
            or report.get("status") not in ("batch_budget_complete","fitting_observed")
            or any(report.get(k) is not True for k in
                ("policy_head_unchanged","buffers_unchanged","checkpoint_roundtrip"))):
        raise ValueError("remote batch lacks a verified eight-epoch receipt")


def train_batch(args, batch, run_id, parent, parent_sha):
    data=batch/"raw-data"
    if not (data/"dataset.json").exists():
        with (batch/"prepare.log").open("ab") as log:
            subprocess.run([sys.executable,"-m","native_runner.training.sspi.tower_shared","prepare",
                "--source-run",str(batch),"--destination",str(data)],check=True,
                stdout=log,stderr=subprocess.STDOUT)
    remote_batch=f"{args.remote_root}/{batch.name}"
    dataset_sha=sha(data/"dataset.json")
    receipt=batch/"training-result.json"
    if receipt.exists():
        report=json.loads(receipt.read_text())
        validate_training_receipt(report,dataset_sha)
        return remote_batch,report
    existing=ssh_python(args,r'''
import json,pathlib,shutil,sys
p=json.loads(sys.argv[1]);root=pathlib.Path(p["path"]);launch=root/"launch.json"
if launch.exists():
 prior=json.loads(launch.read_text());assert prior["dataset_sha"]==p["dataset_sha"]
 print(json.dumps(dict(launched=True)))
else:
 (root/"data").mkdir(parents=True,exist_ok=True)
 assert shutil.disk_usage(root).free>=8*1024**3,"remote disk reserve reached"
 print(json.dumps(dict(launched=False)))
''',dict(path=remote_batch,dataset_sha=dataset_sha))
    if not existing["launched"]:
        subprocess.run(["rsync","-az",str(data)+"/",f"{args.remote}:{remote_batch}/data/"],check=True)
    payload=dict(root=remote_batch,source=args.remote_root+"/code/firstlight_CR",python=args.remote_python,
        source_sha=source_sha(),dataset_sha=dataset_sha,run_id=run_id,parent=parent,parent_sha=parent_sha)
    ssh_python(args,LAUNCH_REMOTE,payload)
    last=None
    while True:
        observed=ssh_python(args,READ_REMOTE,payload)
        if observed["status"]!=last:
            last=observed["status"]
            _atomic_json(args.series_dir/"status.json",dict(status="running",stage="remote_training",
                batch=batch.name,remote_status=last))
            print(json.dumps(dict(stage="remote_training",batch=batch.name,remote_status=last)),flush=True)
        if observed["report"] is not None:
            report=observed["report"]
            validate_training_receipt(report,dataset_sha)
            _atomic_json(receipt,report)
            # A selected model AND its AdamW state remain the next batch's parent.
            return remote_batch,report
        if not observed["alive"] or (last and last.get("status") in ("paused_failure","paused_user")):
            raise RuntimeError("remote trainer stopped without a completed eight-epoch receipt")
        time.sleep(30)


def compatible_series_identity(previous, current):
    mutable = {"coordinator_sha", "coordinator_sha_history"}
    return ({key:value for key,value in previous.items() if key not in mutable}
        == {key:value for key,value in current.items() if key not in mutable})


def run(args):
    config=dict(schema="sspi-tower-refresh-series.v1",series_id=args.series_id,
        coordinator_sha=sha(__file__),
        source_sha=source_sha(),initial_file_sha=sha(args.initial_model),
        parent_checkpoint=args.parent_checkpoint,parent_sha=args.parent_sha,
        excluded_seeds_sha=sha(args.excluded_seeds),max_epochs_per_batch=8,
        probe_sha=sha(args.probe),
        local_games=dict(training=32,validation=8),collection_policy="frozen-original-random",
        remote=args.remote,remote_root=args.remote_root,remote_python=args.remote_python)
    path=args.series_dir/"series.json"
    if path.exists():
        previous=json.loads(path.read_text())
        if previous != config:
            if not compatible_series_identity(previous,config):
                raise ValueError("series identity differs")
            history=previous.get("coordinator_sha_history",[previous.get("coordinator_sha")])
            config["coordinator_sha_history"]=list(dict.fromkeys([*history,config["coordinator_sha"]]))
    _atomic_json(path,config)
    excluded=json.loads(args.excluded_seeds.read_text())
    parent,parent_sha=args.parent_checkpoint,args.parent_sha
    index=1
    while True:
        if (args.series_dir/"STOP_AFTER_BATCH").exists():
            _atomic_json(args.series_dir/"status.json",dict(status="paused_user",next_batch=index))
            return
        if shutil.disk_usage(args.series_dir).free<8*1024**3:
            raise RuntimeError("local disk reserve reached; preserving completed data and checkpoints")
        batch=args.series_dir/f"batch-{index:04d}"
        run_id=f"{args.series_id}-b{index:04d}"
        seed_path=args.series_dir/f"excluded-before-{index:04d}.json"
        if seed_path.exists() and json.loads(seed_path.read_text())!=excluded:
            raise ValueError("seed exclusion history differs")
        _atomic_json(seed_path,excluded)
        # The first batch may already be collecting using the same initial exclusion list.
        effective_excluded=args.excluded_seeds if index==1 else seed_path
        _atomic_json(args.series_dir/"status.json",dict(status="running",stage="local_collection",batch=batch.name))
        collect_batch(args,batch,run_id,effective_excluded)
        remote_batch,report=train_batch(args,batch,run_id,parent,parent_sha)
        planned=json.loads((batch/"run_config.json").read_text())["planned_games"]
        excluded.extend({k:r[k] for k in ("actual_match_seed","actual_rollout_seed")} for r in planned)
        parent,parent_sha=remote_batch+"/run/best.pt",report["checkpoint_sha"]
        if report["status"]=="fitting_observed":
            continuation=dict(status="running",stage="fitting_observed_continue",
                batch=batch.name,next_batch=index+1,checkpoint_sha=parent_sha,independent_acceptance=False)
            _atomic_json(args.series_dir/"status.json",continuation)
            print(json.dumps(continuation),flush=True)
        index+=1


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("series-dir","frozen-source","initial-model","excluded-seeds","probe"):
        p.add_argument("--"+name,type=Path,required=True)
    for name in ("series-id","remote","remote-root","remote-python","parent-checkpoint","parent-sha"):
        p.add_argument("--"+name,required=True)
    p.add_argument("--actual-port",type=int,default=26889)
    p.add_argument("--belief-port",type=int,default=26893)
    p.add_argument("--belief-port-owner-1",type=int,default=26894)
    args=p.parse_args();args.series_dir.mkdir(parents=True,exist_ok=True)
    os.environ["PYTHONPATH"]=str(args.frozen_source/"firstlight_CR")
    os.environ["CR_PROBE"]=str(args.probe)
    with (args.series_dir/"coordinator.lock").open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            run(args)
        except BaseException as error:
            _atomic_json(args.series_dir/"status.json",dict(status="paused_failure",
                error=type(error).__name__,reason=str(error),traceback=traceback.format_exc()))
            raise


if __name__=="__main__":
    main()
