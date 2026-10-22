"""Run-scoped mirror PPO fleet; no credentials, deletes, or historical writes."""
import os,sys,json,pathlib,hashlib,subprocess,concurrent.futures,time,fcntl,traceback,shlex,shutil
ROOT=pathlib.Path(__file__).resolve().parent
CFG=json.loads((ROOT/'fleet.json').read_text()); FLEET=CFG['fleet']; CODE=ROOT/'code'; PY=CODE/'.venv/bin/python'
ENV={**os.environ,'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','CUDA_VISIBLE_DEVICES':'0'}
def event(name,**kw):
 state=dict(event=name,time=time.time(),**kw);print(json.dumps(state),flush=True)
 if name!='host-barrier':
  temp=ROOT/'status.tmp';temp.write_text(json.dumps(state));temp.replace(ROOT/'status.json')
def stop_check(phase):
 if (ROOT/'STOP').exists():
  event('stopped',phase=phase);raise SystemExit(0)
def checked(args,**kw):return subprocess.run(args,check=True,**kw)
def ssh(h,command,**kw):
 if h=='34':return checked(['bash','-c',command],**kw)
 key='firstlight-worker-link' if h in ('35','37') else 'firstlight-capture-link'
 return checked(['ssh','-i',str(pathlib.Path.home()/'.ssh'/key),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=20','-o','ServerAliveInterval=30',f'zhichun@172.16.48.{h}',command],**kw)
def transport(h,src,dst,pull=False):
 key='firstlight-worker-link' if h in ('35','37') else 'firstlight-capture-link'
 remote=f'zhichun@172.16.48.{h}:'
 checked(['rsync','-a','--ignore-existing','-e',f'ssh -i {pathlib.Path.home()}/.ssh/{key} -o BatchMode=yes -o StrictHostKeyChecking=yes',remote+src if pull else src,dst if pull else remote+dst])
def publish(path):
 digest=hashlib.sha256(path.read_bytes()).hexdigest();dst=ROOT/'weights'/f'{digest}.pt'
 if dst.exists():assert dst.read_bytes()==path.read_bytes()
 else:os.link(path,dst)
 return dst

def rollout_host(h,iteration,weight):
 f=FLEET[h];r=f['run']; it=f'{r}/iteration-{iteration:06d}'
 ssh(h,'mkdir '+shlex.quote(it))
 if h!='34':transport(h,str(weight),r+'/weights/'+weight.name)
 check="import pathlib,hashlib,json,shutil; r=pathlib.Path("+repr(r)+"); assert shutil.disk_usage(r).free>1500000000, 'disk space guard'; m=json.loads((r/'manifest.json').read_text()); assert all(hashlib.sha256((r/'code'/p).read_bytes()).hexdigest()==v for p,v in m.items()); assert hashlib.sha256((r/'weights'/"+repr(weight.name)+").read_bytes()).hexdigest()=="+repr(weight.stem)
 ssh(h,f'{r}/code/.venv/bin/python -c '+shlex.quote(check))
 def worker(pair):
  j,port=pair;time.sleep(j*0.5);attempts=[]
  for attempt in range(3):
   out=f'{it}/port-{port}-attempt-{attempt}';seed=CFG['seed_base']+iteration*100000+int(h)*1000+j+attempt*100000000
   assert 0<=seed<2**32
   argv=['--checkpoint',r+'/weights/'+weight.name,'--out-dir',out,'--port',str(port),'--matches','1','--seed',str(seed),'--max-wait-ticks','300','--max-advance-ticks','120','--max-seconds','1800','--shards-per-file','1','--margin-beta','0.05','--deck0','golem-beatdown','--deck1','golem-beatdown','--device','cpu']
   hook=f'import torch,runpy; torch.manual_seed({seed}); runpy.run_path("tools/event_rl_rollout.py",run_name="__main__")'
   cmd=f'cd {r}/code && mkdir {out} && env OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 CUDA_VISIBLE_DEVICES= .venv/bin/python -c '+shlex.quote(hook)+' '+shlex.join(argv)+f' > {out}/collector.log 2>&1'
   try:
    ssh(h,cmd,timeout=2000)
    return dict(port=port,success=True,seed=seed,out=out,attempts=attempts)
   except Exception as e:attempts.append(dict(attempt=attempt,seed=seed,out=out,error=str(e)))
  return dict(port=port,success=False,attempts=attempts)
 with concurrent.futures.ThreadPoolExecutor(len(f['ports'])) as ex:results=list(ex.map(worker,enumerate(f['ports'])))
 target=ROOT/f'iteration-{iteration:06d}'/'collected'/('host-'+h);target.mkdir(parents=True)
 for result in results:
  if not result['success']:continue
  dst=target/f"port-{result['port']}"
  if h=='34':shutil.copytree(result['out'],dst)
  else:transport(h,result['out']+'/',str(dst)+'/',pull=True)
 event('host-barrier',host=h,results=results)
 assert all(v['success'] for v in results),f'host {h} has failed collectors'
 return results

def update(cache,weight,out,shards,log):
 assert not out.exists()
 args=[str(PY),'tools/event_rl_update.py','--cache-root',str(cache),'--checkpoint',str(weight),'--output',str(out),'--max-shards',str(shards),'--epochs','1','--lr','0.00001','--device','cuda:0','--minibatch-lanes','0','--target-kl','0.01','--sequence-chunk-steps','32','--bc-coefficient','0','--kl-coefficient','0','--expected-temperatures','1,1,1,1']
 with log.open('x') as stream:checked(args,cwd=CODE,env=ENV,stdout=stream,stderr=subprocess.STDOUT)
 records=[json.loads(l) for l in log.read_text().splitlines() if l.startswith('{')]
 assert any(x.get('event')=='event-preflight' and x.get('passed') for x in records)
 assert any(x.get('event')=='ppo-update' for x in records)
 verify='from native_runner.training.event_checkpoint import load_event_checkpoint; import sys,torch; x=load_event_checkpoint(sys.argv[1]); parent=load_event_checkpoint(sys.argv[2]); assert x.model.backbone.config.snapshot_only; assert x.payload["parent_checkpoint_sha256"]==parent.checkpoint_sha256; assert all(torch.isfinite(p).all() for p in x.model.parameters()); state=x.payload["event_ppo_training"]; previous=parent.payload.get("event_ppo_training",{}); assert state["optimizer_steps"]==previous.get("optimizer_steps",0)+1; assert state["iterations"]==previous.get("iterations",0)+1; print(x.checkpoint_sha256)'
 checked([str(PY),'-c',verify,str(out),str(weight)],cwd=CODE,env=ENV)
 event('update-verified',records=records)
 return publish(out)

def main():
 lock=(ROOT/'orchestrator.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 (ROOT/'orchestrator.pid').write_text(str(os.getpid()))
 weight=ROOT/'weights'/f"{CFG['initial_checkpoint_sha256']}.pt"
 assert hashlib.sha256(weight.read_bytes()).hexdigest()==CFG['initial_checkpoint_sha256']
 smoke=json.loads((ROOT/'smoke-success.json').read_text())
 assert smoke['initial_checkpoint_sha256']==CFG['initial_checkpoint_sha256']
 assert smoke['manifest_sha256']==hashlib.sha256((ROOT/'manifest.json').read_bytes()).hexdigest()
 assert smoke['optimizer_steps']==2
 iterations=CFG['iterations']; assert isinstance(iterations,int) and iterations>0
 for iteration in range(1,iterations+1):
  stop_check('before-collection')
  event('iteration-start',iteration=iteration,checkpoint=str(weight))
  # Each host creates its unique iteration dir, including learner.
  with concurrent.futures.ThreadPoolExecutor(5) as ex:
   fs=[ex.submit(rollout_host,h,iteration,weight) for h in FLEET]
   results=[f.result() for f in fs]
  it=ROOT/f'iteration-{iteration:06d}';cache=it/'collected';summaries=list(cache.glob('**/part-*.json'));expected=sum(len(x['ports']) for x in FLEET.values())
  assert len(summaries)==expected,(len(summaries),expected)
  event('fleet-barrier',iteration=iteration,shards=len(summaries),hosts=list(FLEET))
  stop_check('before-update')
  event('update-start',iteration=iteration,checkpoint=str(weight))
  weight=update(cache,weight,it/'updated.pt',expected,it/'update.log')
  (it/'success.json').write_text(json.dumps({'checkpoint':str(weight),'shards':expected,'iteration':iteration}))
  event('iteration-success',iteration=iteration,checkpoint=str(weight),shards=expected)
 event('completed',iterations=iterations,checkpoint=str(weight))
if __name__=='__main__':
 try:main()
 except Exception:
  event('fatal',traceback=traceback.format_exc());raise
