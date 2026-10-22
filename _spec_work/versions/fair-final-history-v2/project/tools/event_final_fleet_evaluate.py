"""Evaluate final RL against frozen milestones on idle fleet engines."""
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import traceback

NAME="expanded-171-vs-140-step30-20260918"
ITERATIONS=[171,140]
SEED_BASE=1680918000
PAIRS_PER_MATCHUP=1080

def write(path,data):
    temp=path.with_suffix(".tmp")
    temp.write_text(json.dumps(data,indent=2)+"\n");temp.replace(path)

def host_run(root):
    c=json.loads((root/"config.json").read_text())
    env={**os.environ,"OMP_NUM_THREADS":"1","OPENBLAS_NUM_THREADS":"1","MKL_NUM_THREADS":"1","CUDA_VISIBLE_DEVICES":""}
    def slot_run(slot):
        for attempt in range(3):
            with (root/f"worker-{slot:02d}-attempt-{attempt}.log").open("x") as log:
                p=subprocess.run([sys.executable,str(root/"tournament.py"),"--root",str(root),"--slot",str(slot)],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
            if p.returncode==0:return {"slot":slot,"attempts":attempt+1}
        raise RuntimeError(f"slot {slot} failed three attempts")
    try:
        with concurrent.futures.ThreadPoolExecutor(len(c["ports"])) as pool:
            futures=[]
            for slot in range(len(c["ports"])):
                futures.append(pool.submit(slot_run,slot));time.sleep(.3)
            results=[f.result() for f in futures]
        write(root/"host-status.json",{"event":"completed","workers":results})
    except BaseException:
        write(root/"host-status.json",{"event":"failed","error":traceback.format_exc()});raise

def run(training):
    spec=importlib.util.spec_from_file_location("fleet",training/"fleet_runtime.py")
    f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
    root=training/NAME;root.mkdir(exist_ok=True)
    lock=(training/"orchestrator.lock").open("a");fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    own=(root/"evaluation.lock").open("a");fcntl.flock(own,fcntl.LOCK_EX|fcntl.LOCK_NB)
    (root/"pid").write_text(str(os.getpid()))
    def state(event,**kw):write(root/"status.json",dict(event=event,time=time.time(),**kw))
    try:
        status=json.loads((training/"status.json").read_text())
        assert status["event"]=="completed" and status["iterations"]==171
        started=time.time();state("preparing",started_at=started)
        sources={}
        for i in ITERATIONS:
            s=json.loads((training/f"iteration-{i:06d}"/"success.json").read_text())
            p=Path(s["checkpoint"])
            if not p.exists():f.transport("37",s["archive_checkpoint"],str(p),pull=True)
            assert hashlib.sha256(p.read_bytes()).hexdigest()==p.stem
            sources[str(i)]=p
        offsets={};count=0
        for h,host in f.FLEET.items():offsets[h]=count;count+=len(host["ports"])
        assert count==108
        config={"iterations":ITERATIONS,"matchups":[[171,i] for i in ITERATIONS[1:]],"total_slots":108,"pairs_per_matchup":PAIRS_PER_MATCHUP,"seed_base":SEED_BASE,"max_advance_ticks":30}
        write(root/"plan.json",{**config,"weights":{i:{"sha256":p.stem,"path":str(p)} for i,p in sources.items()},"started_at":started})
        def prepare(h):
            host=f.FLEET[h];remote=host["run"]+"/"+NAME
            f.ssh(h,"mkdir -p "+shlex.quote(remote),timeout=30)
            if h!="34":
                for p in sources.values():f.transport(h,str(p),host["run"]+"/weights/"+p.name)
                for name in ["tournament.py","host_driver.py"]:
                    f.transport(h,str(root/name),remote+"/"+name)
            cfg={**config,"slot_offset":offsets[h],"ports":host["ports"],"code":host["run"]+"/code","weights":{i:{"sha256":p.stem,"path":host["run"]+"/weights/"+p.name} for i,p in sources.items()}}
            code="from pathlib import Path;Path("+repr(remote+"/config.json")+").write_text("+repr(json.dumps(cfg))+")"
            f.ssh(h,"python3 -c "+shlex.quote(code),timeout=30)
            preflight="""import json,socket,sys,hashlib
from pathlib import Path
r=Path(sys.argv[1]);c=json.loads((r/'config.json').read_text())
def control(port,cmd):
 with socket.create_connection(('127.0.0.1',port),timeout=5) as s:
  s.settimeout(15);s.sendall((cmd+chr(10)).encode());b=b''
  while not b.endswith(bytes([10])):
   x=s.recv(65536)
   if not x:break
   b+=x
  return json.loads(b)
checks=[]
for port in c['ports']:
 status=control(port,'engine-status');attest=control(port,'attest')['attestation']
 if status['occupied']:
  assert control(port,'multi-stop')['ok']
  status=control(port,'engine-status')
 assert status['ok'] and not status['occupied'] and attest['production_ready'],(port,status,attest)
 checks.append({'port':port,'status':status,'attestation':attest})
for w in c['weights'].values():assert hashlib.sha256(Path(w['path']).read_bytes()).hexdigest()==w['sha256']
(r/'engine-preflight.json').write_text(json.dumps(checks,indent=2))
print('READY',len(checks))
"""
            f.ssh(h,"python3 -c "+shlex.quote(preflight)+" "+shlex.quote(remote),timeout=180)
            return h,remote
        with concurrent.futures.ThreadPoolExecutor(5) as pool:locations=dict(pool.map(prepare,f.FLEET))
        (root/"collected").mkdir(exist_ok=True)
        state("evaluating",started_at=started,counts={})
        def evaluate(h):
            remote=locations[h]
            cmd="cd "+shlex.quote(remote)+" && "+shlex.quote(f.FLEET[h]["run"]+"/code/.venv/bin/python")+" host_driver.py --host "+shlex.quote(remote)+" > host.log 2>&1"
            f.ssh(h,cmd,timeout=7200)
            if h!="34":f.transport(h,remote+"/",str(root/"collected"/h)+"/",pull=True)
            return h
        with concurrent.futures.ThreadPoolExecutor(5) as pool:
            futures={pool.submit(evaluate,h):h for h in f.FLEET}
            while not all(x.done() for x in futures):
                counts={}
                for h,remote in locations.items():
                    code="from pathlib import Path;import json;r=Path("+repr(remote)+");print(json.dumps({p.name:len(list(p.glob('pair-*-owner-?.json'))) for p in r.glob('RL-*-vs-RL-*')}))"
                    response=f.ssh(h,"python3 -c "+shlex.quote(code),capture_output=True,text=True,timeout=30)
                    for label,n in json.loads(response.stdout).items():counts[label]=counts.get(label,0)+n
                state("evaluating",started_at=started,counts=counts)
                for future in futures:
                    if future.done():future.result()
                time.sleep(20)
            for future in futures:future.result()
        sys.path.insert(0,str(root))
        from tournament_stats import summarize
        reports={};restarts=0
        for h in f.FLEET:
            location=root if h=="34" else root/"collected"/h
            hs=json.loads((location/"host-status.json").read_text());assert hs["event"]=="completed"
            restarts+=sum(x["attempts"]-1 for x in hs["workers"])
        for opponent in ITERATIONS[1:]:
            label=f"RL-171-vs-RL-{opponent:03d}";records=[]
            for h in f.FLEET:
                location=root if h=="34" else root/"collected"/h
                records.extend(json.loads(p.read_text()) for p in (location/label).glob("pair-*-owner-?.json"))
            records.sort(key=lambda x:(x["pair_id"],x["rl_owner"]))
            assert len(records)==PAIRS_PER_MATCHUP*2 and all(x["a"]==171 and x["b"]==opponent for x in records)
            summary=summarize(records,PAIRS_PER_MATCHUP);summary.update(a=171,b=opponent)
            reports[label]=summary
            write(root/(label+".json"),records)
        report={"config":json.loads((root/"plan.json").read_text()),"results":reports,"started_at":started,"completed_at":time.time(),"worker_restarts":restarts}
        write(root/"report.json",report);state("completed",games=PAIRS_PER_MATCHUP*2*(len(ITERATIONS)-1),worker_restarts=restarts)
    except BaseException:
        state("failed",error=traceback.format_exc());raise

if __name__=="__main__":
    if sys.argv[1]=="--host":host_run(Path(sys.argv[2]))
    else:run(Path(sys.argv[1]))
