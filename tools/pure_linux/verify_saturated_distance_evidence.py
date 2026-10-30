"""Recompute one pinned experiment from operator-held, original raw evidence."""
import hashlib,json,sys,statistics,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from compare_headless_snapshots import compare
from run_headless_match import verify_completion,BOOT
from summarize_headless_benchmark import summarize,records
from summarize_headless_threeway import validate_terminal_archive
from compare_headless_snapshots import inspect_archive

def main():
    import argparse
    parser=argparse.ArgumentParser(description='Recompute fixed saturated-distance complete-frame and serial timing evidence.')
    parser.add_argument('archive_root',type=Path)
    parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    base=args.archive_root
    root=base/'nashura-saturated-distance-20261031-v2'
    old=args.reference
    read=lambda p:json.loads(p.read_text())
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    assert read(root/'correctness-done.json')=={'runs':8,'successes':8}
    assert read(root/'done.json')=={'runs':32,'successes':32}
    assert (base/'metadata/nashura-saturated-distance-control-20261031-v2.exit').read_text().strip()=='0'
    assert (root/'instruction-check.exit').read_text().strip()=='0'
    assert 'SATURATED_DISTANCE_CHECK cases=2234256 original_and_candidate=equal conditional_y_read=preserved' in (root/'instruction-check.stdout').read_text()
    expected_cases=['deck-'+deck+'-seed-'+seed for deck in ('a','b') for seed in ('1','7')]
    assert sorted(p.name for p in root.glob('deck-*'))==expected_cases
    failed=base/'nashura-saturated-distance-20261031-v1'
    for case in expected_cases:
     for variant in ('original','distance'):
      directory=failed/case/variant
      assert (directory/'exit.txt').read_text().strip()=='70'
      assert 'not accessible for the namespace' in (directory/'stderr.log').read_text()
    prefixes=('HEADLESS_BATTLE_OBSERVATION ','HEADLESS_BATTLE_PLAYER ','HEADLESS_BATTLE_OBJECT ')
    rows=[];correct=[]
    for case in sorted(root.glob('deck-*')):
     settings=read(case/'settings.json');before=read(old/case.name/'settings.json')
     assert all(settings[k]==v for k,v in before.items() if k not in ('observer','identities'))
     assert all(settings['identities'][k]==v for k,v in before['identities'].items())
     assert settings['identities'][settings['observer']]=='6e397f814aff95b0d45a378d91cff069032183dcfc3d0e079d57553025fbfc5d'
     assert settings['identities'][settings['replay']]==sha(Path(__file__).with_name('fixtures')/('headless-runtime-'+case.name+'.json'))
     expected=[x for x in (old/case.name/'full-frame/stdout.log').read_text().splitlines() if x.startswith(prefixes)]
     for label in ('original','distance'):
      p=case/label;log=(p/'stdout.log').read_text();tick=verify_completion(int((p/'exit.txt').read_text()),log)
      pair=compare(old/case.name/'full-frame/snapshots.bin',p/'snapshots.bin',tick);assert pair['equal']
      observed=[x for x in log.splitlines() if x.startswith(prefixes)];assert observed and observed==expected
      plan=read(p/'plan.json');assert plan['identities']==settings['identities'] and plan['snapshot_trace']
      assert plan['native_optimization']==(None if label=='original' else 'saturated-distance-v1')
      markers=[x for x in log.splitlines() if x.startswith('HEADLESS_NATIVE_OPTIMIZATION ')]
      assert markers==(['HEADLESS_NATIVE_OPTIMIZATION name=saturated-distance-v1 installed=1 functions=2'] if label=='distance' else [])
      assert read(p/'result.json')['archive_sha256']==pair['right']['sha256']
      correct.append({'case':case.name,'variant':label,'final_tick':tick,'frames':pair['records'],'archive_sha256':pair['right']['sha256'],'archive_bytes':pair['right']['bytes'],'observed_lines':len(observed),'full_frames_equal':True,'observed_equal':True})
     resets=records((case/'original/stdout.log').read_text(),'HEADLESS_BATTLE_RESET');assert len(resets)==1
     reset=resets[0];tick=read(case/'original/result.json')['final_tick']
     validate_terminal_archive(case/'original/snapshots.bin',inspect_archive(case/'original/snapshots.bin',tick),reset)
     runs=[]
     for index,label in enumerate(('original','distance','distance','original','distance','original','original','distance'),1):
      p=case/'timing'/(str(index).zfill(2)+'-'+label);log=(p/'stdout.log').read_text();plan=read(p/'plan.json')
      assert verify_completion(int((p/'exit.txt').read_text()),log,216)==tick
      assert plan['benchmark_repeats']==216 and plan['step_sample_every']==16 and plan['serial_batches']==1
      assert not any(plan[k] for k in ('thread_resources','native_phases','snapshot_trace','record_hand_actions'))
      assert plan['command'][:4]==['timeout','-k','3s','120s'] and plan['identities']==settings['identities']
      expected_command=['timeout','-k','3s','120s','unshare','-Ur','-m','-n','-i','/bin/sh',settings['namespace'],settings['closure'],settings['linker_config'],'/'+str(Path(settings['runtime']).relative_to(settings['closure'])),'-Xbootclasspath:'+':'.join(BOOT),'-Djava.library.path=/data/local/tmp/libg-input','--probe-args','/data/local/tmp/libg-input/libg.so','/data/local/tmp/libart_engine_jni_probe.so','/oldroot'+settings['apk'],'/oldroot'+plan['output']+'/probe-data','callbacks','apk-context','/oldroot'+settings['observer'],'native-create','native-destroy','native-active','game-state-action-step','/oldroot'+settings['replay'],settings['identities'][settings['replay']]]
      assert plan['command']==expected_command and Path(plan['output']).parts[-3:]==(case.name,'timing',p.name)
      env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C','CLASSPATH':'/oldroot'+settings['probe_dex']+':/data/local/tmp/current-game-classes.dex','NASHURA_BATTLE_STEPS':'10000','NASHURA_SNAPSHOT_ADAPTER':'1','NASHURA_QUIET_SNAPSHOTS':'1','NASHURA_BENCHMARK_VALIDATE':'1','NASHURA_QUIET':'1','NASHURA_BENCHMARK_MINIMAL':'1','NASHURA_RESET_REPEATS':'216','NASHURA_STEP_SAMPLE_EVERY':'16','NASHURA_MEMORY_SAMPLE':'1','NASHURA_TASK_TIMEOUT_MS':'120000'}
      if label=='distance':env['NASHURA_NATIVE_OPTIMIZATION']='saturated-distance-v1'
      assert env==plan['environment']
      markers=[x for x in log.splitlines() if x.startswith('HEADLESS_NATIVE_OPTIMIZATION ')]
      assert markers==(['HEADLESS_NATIVE_OPTIMIZATION name=saturated-distance-v1 installed=1 functions=2'] if label=='distance' else [])
      assert all(not x.startswith(('HEADLESS_BATTLE_OBSERVATION ','HEADLESS_BATTLE_PLAYER ','HEADLESS_BATTLE_OBJECT ','HEADLESS_NATIVE_PHASE','HEADLESS_WARM_RESOURCES ')) for x in log.splitlines())
      runtime=read(case/'timing'/(p.name+'-environment.json'));assert runtime['cpu_count']==2 and runtime['affinity']==[0,1]
      s=summarize(log,216,expected_tick=tick,expected_digest=reset['digest'],expected_bytes=int(reset['bytes']),warmup_matches=16)
      for k in ('samples','memory_samples','step_sample_phase_by_match','step_sample_phase_period_by_match'):s.pop(k)
      g=read(case/'timing'/(p.name+'-summary.json'))
      def equal(a,b):
       if isinstance(a,dict):return a.keys()==b.keys() and all(equal(a[k],b[k]) for k in a)
       if isinstance(a,float):return math.isclose(a,b,rel_tol=1e-14,abs_tol=1e-12)
       return a==b
      assert equal(s,g)
      runs.append({'index':index,'variant':label,'summary':s,'stdout_sha256':sha(p/'stdout.log'),'plan_sha256':sha(p/'plan.json')})
     def aggregate(items):
      med=statistics.median(x['summary']['median_match_ms'] for x in items);sec=sum(x['summary']['timed_seconds'] for x in items)
      return {'batch_count':len(items),'median_batch_match_ms':med,'timed_seconds':sec,'ticks_per_second':len(items)*200*tick/sec,'matches_per_second':len(items)*200/sec,'step_percentiles_us':{k:statistics.median(x['summary']['step_percentiles_us'][k] for x in items) for k in ('p50','p95','p99')}}
     paths={label:aggregate([x for x in runs if x['variant']==label]) for label in ('original','distance')}
     blocks=[]
     for b in (runs[:4],runs[4:]):
      a=aggregate([x for x in b if x['variant']=='original']);n=aggregate([x for x in b if x['variant']=='distance']);blocks.append((n['median_batch_match_ms']/a['median_batch_match_ms']-1)*100)
     rows.append({'case':case.name,'paths':paths,'match_ms_change_percent':(paths['distance']['median_batch_match_ms']/paths['original']['median_batch_match_ms']-1)*100,'throughput_change_percent':(paths['distance']['ticks_per_second']/paths['original']['ticks_per_second']-1)*100,'interleaved_block_match_ms_changes_percent':blocks,'runs':runs})
    assert len(rows)==4 and len(correct)==8 and sum(len(x['runs']) for x in rows)==32
    out={'host_recomputed':True,'matches':6912,'measured_matches':6400,'correctness':correct,'cases':rows,'initial_namespace_failure_preserved':True,'whole_goal_complete':False}
    with args.output.open('x') as f:json.dump(out,f,indent=2);f.write(chr(10))
    for c in rows:print(c['case'],c['match_ms_change_percent'],c['throughput_change_percent'],c['interleaved_block_match_ms_changes_percent'])


if __name__ == '__main__':
    main()
