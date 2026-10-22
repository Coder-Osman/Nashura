"""Post-training fixed-budget paired evaluation against the original RL-171."""
import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import time
import traceback


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def host_run(root):
    import subprocess
    config = json.loads((root/'config.json').read_text())
    env = {**os.environ, 'OMP_NUM_THREADS':'1', 'OPENBLAS_NUM_THREADS':'1',
           'MKL_NUM_THREADS':'1', 'CUDA_VISIBLE_DEVICES':''}
    def worker(slot):
        for attempt in range(3):
            with (root/f'worker-{slot:02d}-attempt-{attempt}.log').open('x') as log:
                p = subprocess.run([sys.executable, str(root/'tournament.py'), '--root', str(root),
                                    '--slot', str(slot)], env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT)
            if p.returncode == 0:
                return {'slot':slot, 'attempts':attempt+1}
        raise RuntimeError(f'evaluation slot {slot} failed with its original seeds')
    try:
        with concurrent.futures.ThreadPoolExecutor(len(config['ports'])) as pool:
            futures = []
            for slot in range(len(config['ports'])):
                futures.append(pool.submit(worker, slot)); time.sleep(.3)
            result = [f.result() for f in futures]
        write(root/'host-status.json', {'event':'completed','workers':result})
    except BaseException:
        write(root/'host-status.json', {'event':'failed','error':traceback.format_exc()})
        raise


def run(training):
    spec = importlib.util.spec_from_file_location('fleet', training/'fleet_runtime.py')
    f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)
    cfg = f.CFG
    parent_lock = (Path(cfg['parent_run'])/'orchestrator.lock').open('a')
    fcntl.flock(parent_lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
    lock = (training/'orchestrator.lock').open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    root = training/'post-training-eval'; root.mkdir(exist_ok=True)
    own = (root/'evaluation.lock').open('a'); fcntl.flock(own,fcntl.LOCK_EX|fcntl.LOCK_NB)
    status = json.loads((training/'status.json').read_text())
    assert status['event']=='completed' and status['iterations']==201 and status['additional_iterations']==30
    started = time.time()
    def state(event,**kwargs):write(root/'status.json',dict(event=event,time=time.time(),**kwargs))
    weights = {'201':Path(status['checkpoint']), '171':training/'weights'/(cfg['baseline_checkpoint_sha256']+'.pt')}
    pairs = 1080
    plan = {'iterations':[201,171], 'matchups':[[201,171]], 'pairs_per_matchup':pairs,
            'seed_base':1780918000, 'total_slots':108, 'max_advance_ticks':30,
            'allow_architecture_comparison':True,
            'weights':{k:{'sha256':p.stem,'path':str(p)} for k,p in weights.items()},
            'scope':'30-round residual successor versus frozen original RL-171; no equal-budget architecture control'}
    write(root/'plan.json', plan)
    offsets={};total=0
    for h,host in f.FLEET.items():offsets[h]=total;total+=len(host['ports'])
    assert total==108
    control_code='''from pathlib import Path
import concurrent.futures,json,socket,sys,hashlib
r=Path(sys.argv[1]);c=json.loads((r/'config.json').read_text())
def command(port,cmd):
 with socket.create_connection(('127.0.0.1',port),timeout=10) as s:
  s.settimeout(30);s.sendall((cmd+chr(10)).encode());b=b''
  while not b.endswith(bytes([10])):
   x=s.recv(65536)
   if not x:break
   b+=x
  return json.loads(b)
def check(port):
 before=command(port,'engine-status')
 if before['occupied']:assert command(port,'multi-stop')['ok']
 after=command(port,'engine-status');assert after['ok'] and not after['occupied']
 assert command(port,'attest')['attestation']['production_ready']
 return {'port':port,'before':before,'after':after}
for w in c['weights'].values():assert hashlib.sha256(Path(w['path']).read_bytes()).hexdigest()==w['sha256']
with concurrent.futures.ThreadPoolExecutor(6) as p:results=list(p.map(check,c['ports']))
print(json.dumps(results))
'''
    try:
        state('preparing',started_at=started)
        for p in weights.values():assert hashlib.sha256(p.read_bytes()).hexdigest()==p.stem
        def prepare(h):
            host=f.FLEET[h];remote=host['run']+'/post-training-eval'
            f.ssh(h,'mkdir -p '+shlex.quote(remote),timeout=30)
            if h!='34':
                for p in weights.values():f.transport(h,str(p),host['run']+'/weights/'+p.name)
            for src,name in [(training/'evaluate.py','host_driver.py'),
                             (training/'code/tools/event_checkpoint_roundrobin.py','tournament.py')]:
                if h=='34':
                    import shutil
                    shutil.copyfile(src,root/name)
                else:f.transport(h,str(src),remote+'/'+name)
            local_config={**plan,'slot_offset':offsets[h],'ports':host['ports'],'code':host['run']+'/code',
                'weights':{k:{'sha256':p.stem,'path':host['run']+'/weights/'+p.name} for k,p in weights.items()}}
            command='from pathlib import Path;Path('+repr(remote+'/config.json')+').write_text('+repr(json.dumps(local_config))+')'
            f.ssh(h,'python3 -c '+shlex.quote(command),timeout=30)
            response=f.ssh(h,'python3 -c '+shlex.quote(control_code)+' '+shlex.quote(remote),capture_output=True,text=True,timeout=120)
            return h,remote,json.loads(response.stdout)
        with concurrent.futures.ThreadPoolExecutor(5) as pool:prepared=list(pool.map(prepare,f.FLEET))
        locations={h:r for h,r,_ in prepared};write(root/'engine-preflight.json',{h:x for h,_,x in prepared})
        def evaluate(h):
            remote=locations[h]
            f.ssh(h,'cd '+shlex.quote(remote)+' && '+shlex.quote(f.FLEET[h]['run']+'/code/.venv/bin/python')+
                  ' host_driver.py --host '+shlex.quote(remote)+' > host.log 2>&1',timeout=7200)
            if h!='34':f.transport(h,remote+'/',str(root/'collected'/h)+'/',pull=True)
        (root/'collected').mkdir(exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(5) as pool:
            futures=[pool.submit(evaluate,h) for h in f.FLEET]
            while not all(x.done() for x in futures):
                counts={}
                for h,remote in locations.items():
                    code='from pathlib import Path;print(len(list(Path('+repr(remote)+').glob("RL-201-vs-RL-171/pair-*-owner-?.json"))))'
                    response=f.ssh(h,'python3 -c '+shlex.quote(code),capture_output=True,text=True,timeout=30)
                    counts[h]=int(response.stdout)
                state('evaluating',games=sum(counts.values()),counts=counts,started_at=started)
                for future in futures:
                    if future.done():future.result()
                time.sleep(20)
            for future in futures:future.result()
        sys.path.insert(0,str(training/'code'))
        from tools.event_rl_fleet_evaluate import summarize
        records=[];restarts=0
        for h in f.FLEET:
            location=root if h=='34' else root/'collected'/h
            hs=json.loads((location/'host-status.json').read_text());assert hs['event']=='completed'
            restarts+=sum(x['attempts']-1 for x in hs['workers'])
            records.extend(json.loads(p.read_text()) for p in (location/'RL-201-vs-RL-171').glob('pair-*-owner-?.json'))
        records.sort(key=lambda x:(x['pair_id'],x['rl_owner']))
        assert len(records)==pairs*2
        for x in records:
            assert x['a_sha256']==weights['201'].stem and x['b_sha256']==weights['171'].stem
            assert x['seed']==plan['seed_base']+x['pair_id']
            assert x['counters']['terminal'] and x['counters']['act_violation']==0
        summary=summarize(records,pairs)
        write(root/'matches.json',records)
        report={'config':plan,'result':summary,'started_at':started,'completed_at':time.time(),'worker_restarts':restarts}
        write(root/'report.json',report)
        low,high=summary['score_rate_95_ci']
        (root/'report.md').write_text('# 残差模型续训 30 轮评估\n\n'
            +f"第 201 轮对原始第 171 轮：{summary['wins']} 胜 / {summary['draws']} 平 / {summary['losses']} 负，"
            +f"胜率 {summary['win_rate']:.2%}，配对 95% 区间 {low:.2%}–{high:.2%}。\n\n"
            +'1,080 对新种子换边局，共 2,160 局；石头人镜像、11 级、温度 1、margin beta=0、最大推进 30 tick。'
            +'此比较衡量迁移后继续训练的总体进步；未运行相同 30 轮预算的旧架构对照，故不能把提升单独归因于残差 MLP。\n')
        cleanup={}
        for h,remote in locations.items():
            response=f.ssh(h,'python3 -c '+shlex.quote(control_code)+' '+shlex.quote(remote),capture_output=True,text=True,timeout=120)
            cleanup[h]=json.loads(response.stdout)
        write(root/'cleanup.json',cleanup)
        state('completed',games=len(records),result=summary,worker_restarts=restarts)
    except BaseException:
        state('failed',error=traceback.format_exc());raise


if __name__=='__main__':
    if sys.argv[1]=='--host':host_run(Path(sys.argv[2]))
    else:run(Path(sys.argv[1]))
