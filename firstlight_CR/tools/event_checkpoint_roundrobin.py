"""Frozen checkpoint tournament on isolated native engine ports."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


def write_json(path, value):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,indent=2)+'\n')
    temp.replace(path)


def jobs(config, slot):
    matchups=config.get('matchups',list(itertools.combinations(config['iterations'],2)))
    for group,(a,b) in enumerate(matchups):
        for pair in range(config.get('slot_offset',0)+slot,config['pairs_per_matchup'],config.get('total_slots',len(config['ports']))):
            seed=config['seed_base']+group*10000+pair
            for owner in (0,1):
                yield a,b,pair,owner,seed


def markdown_report(report):
    iterations=report['config']['iterations']
    matrix={}
    details=[]
    for summary in report['results'].values():
        a,b=summary['a'],summary['b']
        n=summary['games']
        matrix[a,b]=summary['wins']/n
        matrix[b,a]=summary['losses']/n
        low,high=summary['score_rate_95_ci']
        details.append(f"| {a} vs {b} | {summary['wins']} / {summary['draws']} / {summary['losses']} | {summary['win_rate']:.2%} | {summary['score_rate']:.2%} | {low:.2%}–{high:.2%} |")
    lines=['# Checkpoint 原生对局评估','',
        '行模型对列模型的胜率（胜局 / 全部对局）：','',
        '| 轮次 | '+' | '.join(map(str,iterations))+' |',
        '|---|'+'---:|'*len(iterations)]
    for a in iterations:
        lines.append('| '+str(a)+' | '+' | '.join('—' if a==b else f'{matrix[a,b]:.2%}' for b in iterations)+' |')
    lines+=['','每组同种子换边，石头人镜像，11 级，采样温度 1。只统计正常终局。','',
        '| 对阵（前者视角） | 胜 / 平 / 负 | 胜率 | 得分率 | 得分率 95% 区间 |',
        '|---|---:|---:|---:|---:|',*details,'',
        '得分率 =（胜局 + 0.5 × 平局）/ 总局数；置信区间使用成对 bootstrap。此结果仅描述上述镜像卡组和对战配置。','']
    return '\n'.join(lines)


def worker(root,slot,config):
    import torch
    sys.path.insert(0,config['code'])
    from tools import event_rl_rollout as r
    torch.set_num_threads(1)
    loaded={}
    for iteration,weight in config['weights'].items():
        checkpoint=r.load_event_checkpoint(weight['path'],device='cpu')
        assert checkpoint.checkpoint_sha256==weight['sha256']
        assert checkpoint.model.backbone.config.snapshot_only
        checkpoint.model.eval()
        loaded[int(iteration)]=checkpoint
    if len({x.contract_sha256 for x in loaded.values()}) != 1:
        assert config.get('allow_architecture_comparison') is True
        contracts=[x.payload['event_checkpoint_contract'] for x in loaded.values()]
        assert len({x['input_semantics'] for x in contracts})==1
        assert len({x['action_semantics'] for x in contracts})==1
        first_loaded=next(iter(loaded.values()))
        assert all(x.model.event_config==first_loaded.model.event_config
                   and x.model.backbone.config==first_loaded.model.backbone.config
                   for x in loaded.values())
    bundle=r.production_semantic_bundle()
    proxies,coordinator=r.build_resident_training_proxies_v4((0,),host='127.0.0.1',port=config['ports'][slot],timeout=90.,step_until_event=True)
    env=r.BattleEnvV1(native=proxies[0],card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=r.RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog),
        ruleset_id=hashlib.sha256(b'checkpoint-roundrobin-native-v1').hexdigest(),terminal_margin_beta=0.)
    deck=r._deck_registry()['golem-beatdown']
    first=True
    for a,b,pair,owner,seed in jobs(config,slot):
        folder=root/f'RL-{a:03d}-vs-RL-{b:03d}'
        folder.mkdir(exist_ok=True)
        dst=folder/f'pair-{pair:04d}-owner-{owner}.json'
        if dst.exists():
            previous=json.loads(dst.read_text())
            assert (previous['a_sha256'],previous['b_sha256'],previous['seed'])==(loaded[a].checkpoint_sha256,loaded[b].checkpoint_sha256,seed)
            continue
        if (root/'STOP').exists():
            raise RuntimeError('Tournament STOP requested')
        torch.manual_seed(seed)
        actor0,actor1=(loaded[a],loaded[b]) if owner==0 else (loaded[b],loaded[a])
        with torch.inference_mode():
            recorder,counters=r.play_match(environment=env,coordinator=coordinator,
                model=actor0.model,opponent_model=actor1.model,event_config=actor0.model.event_config,
                catalog=actor0.model.backbone.catalog,device=torch.device('cpu'),decks={0:deck,1:deck},
                match_id=f'rr-{a}-{b}-{seed}-{owner}',policy_version=actor0.checkpoint_sha256,
                seed=seed,env_id=0,temperatures=(1.,1.,1.,1.),max_ticks=7200,max_seconds=1800.,
                max_advance_ticks=config.get('max_advance_ticks',120),max_wait_ticks=300,first_match=first,
                behavior_checkpoint_sha256=actor0.checkpoint_sha256,behavior_model_contract_hash=actor0.contract_sha256)
        first=False
        # Check actual done flags, not just the driver's terminal-or-truncated flag.
        lane=recorder.rows[0]
        assert counters['terminal'] and lane and lane[-1].done and not any(x.truncated for x in lane)
        result={int(k):v for k,v in counters['results'].items()}
        assert set(result)=={0,1} and sum(result.values())==0
        assert all(v in (-1.,0.,1.) for v in result.values())
        reward=result[owner]
        record={'a':a,'b':b,'a_sha256':loaded[a].checkpoint_sha256,'b_sha256':loaded[b].checkpoint_sha256,
            'contract_sha256':actor0.contract_sha256,'pair_id':pair,'rl_owner':owner,'seed':seed,
            'reward':reward,'score':(reward+1)/2,'port':config['ports'][slot],'counters':counters}
        write_json(dst,record)
        print(json.dumps({'a':a,'b':b,'pair_id':pair,'a_owner':owner,'reward_a':reward,'tick':counters['final_tick']}),flush=True)
        del recorder,counters,record,lane


def run(root,config):
    from tournament_stats import summarize
    lock=(root/'tournament.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    (root/'pid').write_text(str(os.getpid()))
    env={**os.environ,'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','CUDA_VISIBLE_DEVICES':''}
    children={};attempts={};reports={};started=time.time()
    def launch(slot):
        attempt=attempts.get(slot,0);attempts[slot]=attempt+1
        with (root/f'worker-{slot:02d}-attempt-{attempt}.log').open('x') as log:
            process=subprocess.Popen([sys.executable,__file__,'--root',str(root),'--slot',str(slot)],env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
        children[slot]=process
    try:
        for slot in range(len(config['ports'])):
            launch(slot);time.sleep(.3)
        while children:
            time.sleep(10)
            for slot,process in list(children.items()):
                code=process.poll()
                if code is None:continue
                del children[slot]
                if code!=0:
                    if attempts[slot]>=3 or (root/'STOP').exists():
                        raise RuntimeError(f'worker {slot} failed after {attempts[slot]} attempts')
                    launch(slot)
            counts={}
            for a,b in itertools.combinations(config['iterations'],2):
                label=f'RL-{a:03d}-vs-RL-{b:03d}'
                paths=list((root/label).glob('pair-*-owner-?.json'))
                counts[label]=len(paths)
                if len(paths)==config['pairs_per_matchup']*2 and label not in reports:
                    records=[json.loads(p.read_text()) for p in sorted(paths)]
                    summary=summarize(records,config['pairs_per_matchup'])
                    summary.update(a=a,b=b,rate_perspective=f'RL-{a:03d}')
                    reports[label]=summary
                    write_json(root/label/'summary.json',summary)
                    print(json.dumps({'event':'matchup-completed','matchup':label,**summary}),flush=True)
            write_json(root/'status.json',{'event':'running','time':time.time(),'started_at':started,'counts':counts,'completed_matchups':list(reports),'worker_attempts':attempts})
        assert len(reports)==6
        report={'config':config,'results':reports,'started_at':started,'completed_at':time.time(),'worker_attempts':attempts}
        write_json(root/'report.json',report)
        (root/'report.md').write_text(markdown_report(report))
        write_json(root/'status.json',{'event':'completed','time':time.time(),'games':sum(x['games'] for x in reports.values()),'report':str(root/'report.json')})
    except BaseException:
        write_json(root/'status.json',{'event':'failed','time':time.time(),'error':traceback.format_exc()})
        raise
    finally:
        for process in children.values():
            if process.poll() is None:process.terminate()
        for process in children.values():
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill()
        # Own guests only; production training uses 5554..5560.
        for guest in json.loads((root/'emulators-cloned.json').read_text()):
            subprocess.run([config['adb'],'-s',guest['serial'],'emu','kill'],capture_output=True,timeout=30)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--slot',type=int)
    args=parser.parse_args();root=args.root.resolve()
    config=json.loads((root/'config.json').read_text())
    if args.slot is None:run(root,config)
    else:worker(root,args.slot,config)


if __name__=='__main__':main()
