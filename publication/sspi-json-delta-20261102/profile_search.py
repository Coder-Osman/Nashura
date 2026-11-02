from pathlib import Path
import functools, importlib.util, json, time, sys, hashlib
from collections import defaultdict

import argparse
parser=argparse.ArgumentParser(description='Profile one dedicated idle native worker; reconfigures the test match')
parser.add_argument('--port',type=int,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--transport',choices=('persistent','persistent-delta'),required=True)
args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[2]
MODE=args.transport
OUT=args.output
if OUT.exists() and any(OUT.iterdir()):raise ValueError('use a new output directory')
OUT.mkdir(parents=True,exist_ok=True)
spec=importlib.util.spec_from_file_location('search_comparison',ROOT/'publication/sspi-json-delta-20261102/search_compare.py')
bench=importlib.util.module_from_spec(spec);spec.loader.exec_module(bench)
bench.OUT=OUT;bench.PORT=args.port
from native_runner import cr_native_env as native
from native_runner.training.fair_final import search, search_runtime, rollout, native as runtime
from native_runner.training.sspi import protocol, model
from native_runner.training.az_joint.model import SemanticEncoder
from native_runner.training.v4 import components, tensors, tensorizer

first_information=[];first_model=[]
stats=defaultdict(lambda:dict(calls=0,inclusive_seconds=0.,exclusive_seconds=0.))
stack=[];active=False;command_stack=[]
wire=defaultdict(lambda:dict(calls=0,bytes=0,parse_seconds=0.,max_bytes=0))

def wrap(obj,name,label):
 original=getattr(obj,name)
 @functools.wraps(original)
 def measured(*args,**kwargs):
  if not active:return original(*args,**kwargs)
  parent=stack[-1][0] if stack else 'search'
  item=[label,0.];stack.append(item);start=time.perf_counter()
  try:return original(*args,**kwargs)
  finally:
   elapsed=time.perf_counter()-start;stack.pop()
   if stack:stack[-1][1]+=elapsed
   row=stats[parent+' > '+label];row['calls']+=1;row['inclusive_seconds']+=elapsed;row['exclusive_seconds']+=elapsed-item[1]
 setattr(obj,name,measured)

for obj,names,prefix in [
 (model.SSPIModel,['forward','encode'],'model'),
 (SemanticEncoder,['forward'],'semantic_encoder'),
 (tensors.TensorRecordV4,['to_model_input'],'frame'),
 (tensors.UniversalSemanticBatchV4,['validate'],'frame'),
 (tensorizer.UniversalObservationTensorizerV4,['tensorize'],'tensorizer'),
 (search_runtime.FrozenSearchInference,['predict','sample'],'policy'),
 (model.SSPIPolicy,['sample'],'policy_distribution'),
 (protocol.SSPILegality,['identity'],'legality'),
 (rollout.FinalRollout,['freeze_due_information','commit_simultaneous'],'rollout'),
 (rollout.RolloutSnapshot,['__init__','restore','close'],'rollout_snapshot'),
 (runtime.FinalSnapshot,['__init__','restore','close'],'runtime_snapshot'),
 (runtime.FinalDecisionRuntime,['advance','_freeze'],'runtime'),
 (runtime.FinalBattleEnv,['advance_reservations','_atomic_observation_pair','observe_dual'],'environment'),
 (search_runtime.RolloutSearchWorld,['transition','root_owner_information','restore_root'],'world'),
 (native.NativeClashEnv,['observe_atomic','_validate_atomic_result'],'native'),
 (native.JsonEventCursor,['validate'],'cursor'),
]:
 for name in names:wrap(obj,name,prefix+'.'+name)
for name in ['MechanicProfileEncoder','ActiveEffectEncoder','GlobalCardEncoder','LocalChildSetEncoder','BattleGroupEncoder','TowerEncoder','CardTokenEncoder','RelationTransformerV4','SpatialScatterEncoder','GenericScalarEncoder','CandidateEncoder']:
 wrap(getattr(components,name),'forward',name)
wrap(components.EffectSemanticEncoder,'all_effects','EffectSemanticEncoder.all_effects')
wrap(search,'semantic_digest','search.semantic_digest')
original_property=protocol.SSPIInformation.sha256
def information_sha(self):
 if active and not first_information:first_information.append(self)
 return original_property.fget(self)
protocol.SSPIInformation._profile_sha=information_sha
wrap(protocol.SSPIInformation,'_profile_sha','information.sha256')
protocol.SSPIInformation.sha256=property(protocol.SSPIInformation._profile_sha)

original_request=native.NativeClashEnv._request
@functools.wraps(original_request)
def request(self,command,**kwargs):
 command_stack.append(command.partition(' ')[0])
 try:return original_request(self,command,**kwargs)
 finally:command_stack.pop()
native.NativeClashEnv._request=request
wrap(native.NativeClashEnv,'_request','native._request')
original_loads=native._fast_json_loads
def loads(payload):
 if not active:return original_loads(payload)
 start=time.perf_counter()
 try:return original_loads(payload)
 finally:
  row=wire[command_stack[-1] if command_stack else 'unknown'];row['calls']+=1;row['bytes']+=len(payload);row['max_bytes']=max(row['max_bytes'],len(payload));row['parse_seconds']+=time.perf_counter()-start
native._fast_json_loads=loads
original_run=search.CompletedBoundarySearch.run
def measured_search(self,*args,**kwargs):
 global active
 active=True;start=time.perf_counter()
 try:return original_run(self,*args,**kwargs)
 finally:
  elapsed=time.perf_counter()-start;active=False
  report=dict(schema='sspi-search-breakdown.v1',seconds=elapsed,transport=MODE,seed=20261103,
   note='Nested inclusive timers overlap; exclusive timers partition instrumented scopes. Additional native requests: zero. Profiling overhead means not a new speed benchmark.',
   stats=dict(stats),wire=dict(wire))
  (OUT/'breakdown.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
search.CompletedBoundarySearch.run=measured_search
original_initialize=bench.s.initialize_fresh_model
def initialize(*args,**kwargs):
 m=original_initialize(*args,**kwargs);first_model.append(m);return m
bench.s.initialize_fresh_model=initialize
bench.run(MODE)
base=json.loads((ROOT/'docs/SSPI_JSON_DELTA_EVIDENCE_2026-11-02.json').read_text())['search'][MODE]
result=json.loads((OUT/f'search-final-{MODE}.json').read_text())
fields=['candidate_distribution','fair_sha256','model_sha256','selected_action','valid_simulations']
comparison={key:base[key]==result[key] for key in fields}
comparison['native_tick_count']=base['native_ticks']['actual_ticks']==result['native_ticks']['actual_ticks']
comparison['rpc_counts']={k:v['calls'] for k,v in base['native_rpc'][0]['commands'].items()}=={k:v['calls'] for k,v in result['native_rpc'][0]['commands'].items()}
comparison['all_equal']=all(comparison.values())
(OUT/'equivalence.json').write_text(json.dumps(comparison,indent=2)+'\n')
print(json.dumps(comparison),flush=True)

# Local post-search analysis uses already-captured input and makes no native calls.
import cProfile,pstats
from io import StringIO
from native_runner.training.fair_search.contracts import semantic_digest
info=first_information[0]
entries=dict(frame=info.frame,hand=info.hand,legality=info.legality.identity())
field_timings={}
for name,value in entries.items():
 start=time.perf_counter()
 for _ in range(10):semantic_digest(value)
 field_timings[name]=(time.perf_counter()-start)/10
profile=cProfile.Profile();profile.enable()
for _ in range(5):original_property.fget(info)
profile.disable();buffer=StringIO();pstats.Stats(profile,stream=buffer).strip_dirs().sort_stats('cumtime').print_stats(24)
(OUT/'hash-profile.txt').write_text(buffer.getvalue())
mechanics=first_model[0].encoder.mechanic_profile_encoder
shapes={name:list(getattr(mechanics,name).shape) for name in ['profile_id','guard_archetype_id','guard_numeric','guard_mask','tags','numeric']}
summary=dict(field_hash_seconds=field_timings,legality_shape=list(info.legality.plays.shape),mechanic_profile_count=mechanics.profile_count,mechanic_tables=shapes)
(OUT/'static-and-hash.json').write_text(json.dumps(summary,indent=2)+chr(10))
print(json.dumps(summary),flush=True)
