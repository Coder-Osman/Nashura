"""Single isolated worker, sequential frozen FAIR roots; not a dual-worker test."""
import json, os, time, hashlib, argparse
from pathlib import Path
import torch
from native_runner.training.sspi import search_probe as s
from native_runner.training.fair_final.opening_belief import OpeningEvidence
from native_runner.training.fair_final.policy import FullAction

OUT = None
PORT = None
SEED=20261103

def run(transport):
    torch.set_num_threads(1);torch.use_deterministic_algorithms(True)
    bundle=s.production_semantic_bundle()
    native=s.LegacySearchNative('127.0.0.1',PORT,timeout=90)
    native._persistent_transport_requested=True
    native.json_event_deltas=transport=='persistent-delta'
    status=native._request('multi-status')
    if status.get('active',False) or status['mode']=='resident-headless':
        slots=status.get('slots',[])
        if any(x['envId']!=0 or not x['ended'] for x in slots):raise RuntimeError('isolated test slot is occupied')
        native.stop_resident_mode()
    env=s.FinalBattleEnv(native=native,card_catalog=bundle.native_card_catalog,
        semantic_subset_contract=bundle.semantic_subset_contract,
        effect_catalog=s.RuntimeEffectCatalog.from_catalog(bundle.native_effect_catalog))
    match=s.MatchConfig(deck0=s.HOG_DECK,deck1=s.HOG_DECK,
        seed=s.split_seed(SEED,'actual-match')%(2**31-1),
        end_tick=s.NATIVE_FINALIZATION_DEADLINE_TICK,**s.tournament_level_kwargs())
    env.reset(match_config=match,options=dict(render_mode='headless',decision_ticks=1,
        event_driven_decisions=False,allow_initial_remaining_runtime_rejections=True))
    rollout=s.make_sspi_rollout(env,catalog=bundle.card_catalog,execution_seed=s.split_seed(SEED,'actual-rollout'))
    info=rollout.freeze_due_information()[0]
    evidence=OpeningEvidence.from_view(rollout.runtime.frozen[0],info,opening_tick=env.warmup_ticks)
    before={k:v.sha256 for k,v in rollout.freeze_due_information().items()}
    with rollout.snapshot(s.BattleEnvSidecar) as snapshot:
        rollout.commit_simultaneous({owner:FullAction() for owner in before})
        while not rollout.runtime.clock.ready:rollout.advance()
        snapshot.restore()
        restored={k:v.sha256 for k,v in rollout.freeze_due_information().items()}
        if restored!=before:raise AssertionError('snapshot changed FAIR or hand state')
    native_snapshot_status=native.status()
    if native_snapshot_status['snapshotHandles']!=0:raise AssertionError('snapshot handle leak')
    native.close_transport()
    model=s.initialize_fresh_model(SEED,device='cpu',bundle=bundle).eval()
    model_sha=s.semantic_digest(model.state_dict())
    controller_sha=s.semantic_digest(dict(model_sha=model_sha,action_schema=s.ACTION_SCHEMA,
        delay_ms=[200,400,600,800,1000],wait_ticks=20,device='cpu'))
    inference=s.TimedSearchInference(s.make_search_inference(model,checkpoint_sha=model_sha,controller_sha=controller_sha))
    counter=s.NativeTickCounter(); natives=[]
    def factory():
        n=s.LegacySearchNative('127.0.0.1',PORT,timeout=90)
        n._persistent_transport_requested=True;n.json_event_deltas=transport=='persistent-delta'
        natives.append(n);return n
    def rollout_factory(environment,*,catalog,execution_seed):
        return s.MeasuredSSPIRollout(environment,catalog=catalog,execution_seed=execution_seed,tick_counter=counter)
    belief=s.TrackedOpeningBeliefProvider(factory,evidence=evidence,catalog=bundle.card_catalog,
        sidecar_factory=s.BattleEnvSidecar,rollout_factory=rollout_factory)
    limits=s.SearchLimits(root_noise=.25,root_noise_mode='training')
    identity=dict(search_protocol_sha=s.budget_search_sha(limits,128),root_belief_prior_sha=belief.sampler_sha,
        checkpoint_blue_sha=model_sha,checkpoint_red_sha=model_sha,controller_blue_sha=controller_sha,controller_red_sha=controller_sha)
    start=time.perf_counter()
    try:
        outcome=s.CompletedBoundarySearch(belief,inference,inference,limits=limits).run(info,
            seed=s.split_seed(SEED,'search'),behavior_seed=s.split_seed(SEED,'behavior'),
            codec=s.ActionCodec(s.HOG_DECK),identity=identity,mode='FULL',simulation_budget=128)
        elapsed=time.perf_counter()-start
        assert outcome.result['valid_simulations']==128
        assert sum(x['visits'] for x in outcome.result['candidates'])==128
        assert belief.last_world.closed
        check=s.LegacySearchNative('127.0.0.1',PORT,timeout=15)
        after=check.status();check.close_transport()
        assert after['snapshotHandles']==0
        result=dict(status='passed',scope='single-isolated-process-sequential-root-and-independent-belief',
            physical_actual_root_preservation_tested=False,transport=transport,seed=SEED,
            seconds=elapsed,simulations=128,valid_simulations=128,
            fair_sha256=info.sha256,model_sha256=model_sha,selected_action=outcome.selected.__dict__,
            candidate_distribution=outcome.result['candidates'],audit=outcome.audit,
            snapshot_fair_roundtrip=before==restored,snapshot_handles_after=after['snapshotHandles'],
            independent_belief_lease_released=belief.last_world.closed,native_ticks=counter.summary(),
            inference=inference.summary(),native_rpc=[x.rpc_profile.summary() for x in natives])
        (OUT/f'search-final-{transport}.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
        print(json.dumps({k:v for k,v in result.items() if k not in ['candidate_distribution','audit','native_rpc']},sort_keys=True),flush=True)
    finally:
        for n in natives:n.close_transport()

if __name__=='__main__':
 parser = argparse.ArgumentParser(description="Reconfigure one dedicated test worker; never use a live actor endpoint")
 parser.add_argument("--port", type=int, required=True)
 parser.add_argument("--output", type=Path, required=True)
 parser.add_argument("--transport", choices=("persistent", "persistent-delta"), required=True)
 args = parser.parse_args()
 OUT, PORT = args.output, args.port
 OUT.mkdir(parents=True, exist_ok=True)
 if (OUT / f"search-final-{args.transport}.json").exists():
     raise ValueError("preserve existing search evidence; use a new output directory")
 run(args.transport)
