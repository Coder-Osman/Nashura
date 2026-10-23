import argparse
from dataclasses import asdict, fields
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.adapter import ADAPTER_ID
from native_runner.training.fair_final.codec import ActionCodec
from native_runner.training.fair_final.contracts import validate_search
from native_runner.training.fair_final.deck_belief import FrozenDeckPrior
from native_runner.training.fair_final.episode import atomic_json, terminal_result
from native_runner.training.fair_final.history import FairHistoryRecorder
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction, LegalPrefixes
from native_runner.training.fair_final.prior_provider import PriorHistorySearchProvider
from native_runner.training.fair_final.recovery import RecoveryJournal
from native_runner.training.fair_final.refill_belief import RefillPriorHistoryBeliefFactory
from native_runner.training.fair_final.rollout import DecisionInformation, FinalRollout
from native_runner.training.fair_final.search import CompletedBoundarySearch, SearchLimits, split_seed
from native_runner.training.fair_final.search_runtime import FrozenSearchInference
from native_runner.training.fair_final.storage import durable_write
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import pack, semantic_digest, unpack
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
SEARCH_SEED = 78103003
BEHAVIOR_SEED = 78103004


def outcome_signature(outcome):
    return dict(result=outcome.result, selected=asdict(outcome.selected), target=outcome.policy_target)


def validate_group(group):
    if set(group) != {"tick", "information", "outcomes", "identity", "pending", "actual_unchanged"}:
        raise ValueError("search boundary fields differ")
    if group["actual_unchanged"] is not True or not group["information"]:
        raise ValueError("search modified actual world or omitted its information")
    if set(group["information"]) != set(group["outcomes"]):
        raise ValueError("simultaneous search group is incomplete")
    if (type(group["tick"]) is not int or group["tick"] < 0
            or not set(group["information"]).issubset({"0", "1"})
            or any(type(owner) is not int or owner not in (0, 1) for owner in group["pending"])
            or len(group["pending"]) != len(set(group["pending"]))
            or set(group["pending"]) & {int(owner) for owner in group["information"]}):
        raise ValueError("search root is not a completed own decision boundary")
    codec = ActionCodec(HOG_DECK)
    for seat, encoded in group["information"].items():
        legality = LegalPrefixes(encoded["card_ids"], unpack(encoded["plays"]), wait=True)
        info = DecisionInformation(int(seat), group["tick"], unpack(encoded["frame"]), legality)
        outcome = group["outcomes"][seat]
        signature = outcome["signature"]
        if info.sha256 != encoded["fair_sha"]:
            raise ValueError("search evidence frame identity differs")
        visits = validate_search(signature["result"], info=info, codec=codec,
            identity=group["identity"], budget=128)
        if signature["result"]["noise_fraction"] != 0:
            raise ValueError("engineering search episode requires frozen zero noise")
        action = FullAction(**signature["selected"])
        expected = [[key, visits[key]] for key in sorted(visits) if visits[key]]
        if (signature["target"] != expected or visits.get(codec.encode(action), 0) <= 0
                or not legality.legal(action)):
            raise ValueError("search target or sampled behavior differs from full visits")
    return group


class SearchTrace:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.committed = []

    def path(self, tick):
        return self.root / f"{tick:08d}.pt.gz"

    def load(self, tick):
        return validate_group(torch.load(io.BytesIO(gzip.decompress(self.path(tick).read_bytes())),
            weights_only=True, map_location="cpu"))

    def write(self, group):
        validate_group(group)
        buffer = io.BytesIO()
        torch.save(group, buffer)
        durable_write(self.path(group["tick"]), gzip.compress(buffer.getvalue(), compresslevel=1, mtime=0))

    def confirm(self, information, actions):
        ticks = {info.tick for info in information.values()}
        if len(ticks) != 1 or set(actions) != set(information):
            raise ValueError("committed search group differs from simultaneous view")
        tick = next(iter(ticks))
        group = self.load(tick)
        if set(group["information"]) != {str(owner) for owner in information}:
            raise ValueError("recovered search group owner coverage differs")
        for owner, info in information.items():
            seat = str(owner)
            if (group["information"][seat]["fair_sha"] != info.sha256
                    or group["outcomes"][seat]["signature"]["selected"] != asdict(actions[owner])):
                raise ValueError("recovered frame or action differs from durable search evidence")
        if self.committed and tick <= self.committed[-1]:
            raise ValueError("duplicate search boundary in resumed actor records")
        self.committed.append(tick)


def main():
    parser = argparse.ArgumentParser()
    for name in ("port", "belief-port"):
        parser.add_argument("--" + name, type=int, required=True)
    for name in ("source", "prior", "puct-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--engine-sha256", required=True)
    parser.add_argument("--phase", choices=("continue",), required=True)
    parser.add_argument("--continuation-input", type=Path, required=True)
    parser.add_argument("--segment-reserve-seconds", type=int, default=1800)
    parser.add_argument("--recovery-groups", type=int, default=3)
    parser.add_argument("--wall-seconds", type=int, default=21600)
    args = parser.parse_args()
    if (args.port == args.belief_port or args.recovery_groups < 1
            or not 0 <= args.segment_reserve_seconds < args.wall_seconds <= 86400):
        raise ValueError("independent engines and bounded validation required")
    args.output.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    started = time.monotonic()
    def deadline():
        if time.monotonic() >= started + args.wall_seconds:
            raise TimeoutError("complete-search-engineering validation deadline")

    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("search episode deployed source differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_before = semantic_digest(source.state_dict())
    inference = FrozenSearchInference(source, checkpoint_sha=source_sha, source_adapter=True,
        controller_sha=semantic_digest(dict(source=source_sha, adapter=ADAPTER_ID, temperature=1.)))
    prior = FrozenDeckPrior.from_payload(json.loads(args.prior.read_text()))
    limits = SearchLimits()
    factory = RefillPriorHistoryBeliefFactory(
        lambda: create_search_native(args.belief_port, transport="compact-delta"), prior=prior,
        catalog=source.policy.backbone.catalog, opponent=inference, sidecar_factory=BattleEnvSidecar)
    trace = SearchTrace(args.output / "search-boundaries")
    journal = None
    natives = []
    recorders = {}
    identity = dict(checkpoint_sha256=source_sha, engine_sha256=args.engine_sha256,
        source_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        controller=FinalBattleEnv.timing_protocol, adapter=ADAPTER_ID)

    def create(inputs, episode_id=None):
        native = create_search_native(args.port, transport="compact-delta")
        natives.append(native)
        environment = FinalBattleEnv(native=native)
        init_fields = {field.name for field in fields(MatchConfig) if field.init}
        environment.reset(match_config=MatchConfig(**{key: value for key, value
            in inputs["match_config"].items() if key in init_fields}), options=inputs["reset_options"])
        if episode_id is not None:
            environment.episode_id = episode_id
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog,
            execution_seed=inputs["execution_seed"])
        recorders.clear()
        for owner, info in rollout.freeze_due_information().items():
            recorders[owner] = FairHistoryRecorder(rollout.runtime.frozen[owner], info,
                opening_tick=environment.warmup_ticks)
        original_commit, original_advance = rollout.commit_simultaneous, rollout.advance

        def commit(actions):
            frozen = rollout.freeze_due_information()
            trace.confirm(frozen, actions)
            original_commit(actions)
            for owner, info in frozen.items():
                recorders[owner].record_decision(info, actions[owner])

        def advance():
            deadline()
            outcomes = original_advance()
            if any(reason != "executed" for reason in outcomes.values()):
                raise ValueError("actual search episode rejected an action")
            observations = environment.observe_dual(policy_features_only=True)
            for owner in (0, 1):
                recorders[owner].observe_tick(observations[owner])
            return outcomes

        rollout.commit_simultaneous, rollout.advance = commit, advance
        return rollout

    def search_group(rollout):
        frozen = rollout.freeze_due_information()
        native = rollout.runtime.environment.native
        original_digest = native._request("state-digest")["sha256"]
        fair = {owner: info.sha256 for owner, info in frozen.items()}
        group = dict(tick=rollout.runtime.clock.tick, information={}, outcomes={}, identity=None,
            pending=sorted(rollout.runtime.clock.pending), actual_unchanged=True)
        for owner, info in sorted(frozen.items()):
            deadline()
            history = recorders[owner].capture(info)
            provider = PriorHistorySearchProvider(factory, history=history, check_deadline=deadline)
            search_identity = dict(search_protocol_sha=limits.sha256, root_belief_prior_sha=provider.sampler_sha,
                checkpoint_blue_sha=source_sha, checkpoint_red_sha=source_sha,
                controller_blue_sha=inference.controller_sha, controller_red_sha=inference.controller_sha)
            if group["identity"] is not None and group["identity"] != search_identity:
                raise ValueError("simultaneous search identities differ")
            group["identity"] = search_identity
            print(json.dumps(dict(phase="root-started",tick=info.tick,owner=owner,
                pending=group["pending"],committed_groups=len(trace.committed))),flush=True)
            try:
                outcome = CompletedBoundarySearch(provider, inference, inference, limits=limits).run(info,
                    seed=split_seed(SEARCH_SEED, info.tick, owner),
                    behavior_seed=split_seed(BEHAVIOR_SEED, info.tick, owner), codec=ActionCodec(HOG_DECK),
                    identity=search_identity, deadline=started + args.wall_seconds)
                if provider.active is not None:
                    raise ValueError("search episode leaked world lease")
            finally:
                provider.close()
            if (native._request("state-digest")["sha256"] != original_digest
                    or {seat: current.sha256 for seat, current in rollout.freeze_due_information().items()} != fair):
                raise ValueError("search read or changed an uncommitted simultaneous action")
            group["information"][str(owner)] = dict(frame=pack(info.frame), card_ids=info.legality.card_ids,
                plays=pack(info.legality.plays), fair_sha=info.sha256, history_sha=history.sha256)
            group["outcomes"][str(owner)] = dict(signature=outcome_signature(outcome), audit=outcome.audit)
            print(json.dumps(dict(phase="root-completed",tick=info.tick,owner=owner,
                seconds=outcome.audit["seconds"],visits=outcome.result["valid_simulations"])),flush=True)
        return validate_group(group)

    try:
        with torch.inference_mode():
            prepared_group = None
            if args.phase == "prepare":
                config = MatchConfig(deck0=list(HOG_DECK), deck1=list(HOG_DECK), seed=78103001,
                    end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs())
                inputs = dict(match_config=asdict(config), execution_seed=78103002,
                    reset_options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                        allow_initial_remaining_runtime_rejections=True))
                rollout = create(inputs)
                journal = RecoveryJournal.create(args.output / "private-audit", rollout,
                    identity=identity, private_inputs=inputs)
            else:
                receipt = json.loads(args.continuation_input.read_text())
                if (receipt["schema"] != "FairFinalSearchContinuationInput/v1"
                        or receipt["status"] not in ("verified_failed_prefix", "verified_checkpoint_prefix")
                        or receipt["training_admissible"] is not False
                        or receipt["gates_closed"] is not False or receipt["identity"] != identity):
                    raise ValueError("continuation input is not a verified matching failed prefix")
                if (receipt["source_sha256"] != manifest
                        or {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in trace.root.glob("*.pt.gz")} != receipt["boundary_sha256"]):
                    raise ValueError("continuation source or durable boundary identity differs")
                journal = RecoveryJournal.recover(args.output / "private-audit", create,
                    expected_identity=identity, expected_manifest_sha=receipt["manifest_sha"])
                rollout = journal.rollout
                if journal.sequence != receipt["records"] or journal.previous_sha != receipt["record_sha"]:
                    raise ValueError("recovery head differs from externally pinned receipt")
                if (journal._proof() != receipt["proof"]
                        or len(trace.committed) != receipt["committed_groups"]
                        or rollout.runtime.environment.episode_id != receipt["episode_id"]):
                    raise ValueError("native continuation replay differs from pinned parent head")
                atomic_json(args.output / "recovery-continuation-report.json", dict(
                    status="passed_replay_only", training_admissible=False, gates_closed=False,
                    input_sha256=hashlib.sha256(args.continuation_input.read_bytes()).hexdigest(),
                    manifest_sha=journal.manifest_sha, record_sha=journal.previous_sha,
                    records=journal.sequence, proof=journal._proof(),
                    committed_groups=len(trace.committed), episode_id=receipt["episode_id"]))
                print(json.dumps(dict(phase="continuation-recovered", tick=rollout.runtime.clock.tick,
                    committed_groups=len(trace.committed), records=journal.sequence)), flush=True)
            while not rollout.runtime.ended:
                deadline()
                if rollout.runtime.failed or rollout.runtime.truncated:
                    raise ValueError("search episode failed or truncated")
                if (rollout.runtime.clock.ready
                        and time.monotonic() - started >= args.wall_seconds - args.segment_reserve_seconds):
                    head = journal.checkpoint()
                    atomic_json(args.output / "segment-report.json", dict(
                        status="checkpointed", training_admissible=False, gates_closed=False,
                        head=head, proof=journal._proof(), committed_groups=len(trace.committed),
                        seconds=time.monotonic()-started))
                    return
                if rollout.runtime.clock.ready:
                    group = prepared_group if prepared_group is not None else search_group(rollout)
                    prepared_group = None
                    path = trace.path(group["tick"])
                    if not path.exists():
                        trace.write(group)
                    else:
                        previous = trace.load(group["tick"])
                        if any(previous["outcomes"][owner]["signature"] != outcome["signature"]
                                for owner, outcome in group["outcomes"].items()):
                            raise ValueError("existing search evidence differs")
                    if args.phase == "prepare" and len(trace.committed) >= args.recovery_groups:
                        receipt = journal.checkpoint()
                        atomic_json(args.output / "restart-receipt.json", receipt)
                        atomic_json(args.output / "prepare-report.json", dict(status="checkpointed",
                            tick=group["tick"], committed_groups=len(trace.committed),training_admissible=False))
                        return
                    actions = {int(owner): FullAction(**outcome["signature"]["selected"])
                        for owner, outcome in group["outcomes"].items()}
                    journal.commit(lambda information, generators: actions)
                    atomic_json(args.output / "progress.json", dict(phase=args.phase,tick=group["tick"],
                        committed_groups=len(trace.committed),seconds=time.monotonic()-started))
                journal.advance()
            observations = rollout.runtime.environment.observe_dual(policy_features_only=True)
            result = terminal_result(observations[0].terminal, rollout.runtime.environment.raw_observation,
                truncated=rollout.runtime.truncated, failed=rollout.runtime.failed)
            evidence = rollout.runtime.clock.evidence
            roots = []
            for tick in trace.committed:
                group = trace.load(tick)
                roots.extend(dict(tick=tick,owner=int(owner),signature=outcome["signature"],
                    pending=group["pending"]) for owner,outcome in group["outcomes"].items())
            if len(roots) != rollout.runtime.clock.next_identity or len(evidence) != len(roots):
                raise ValueError("complete search episode contains missing decisions or outcomes")
            if any(entry["reason"] not in ("executed","wait_expired","preempted","terminal_expired")
                    or entry["reservation"] != index or entry["owner"] != roots[index]["owner"]
                    for index,entry in enumerate(sorted(evidence,key=lambda entry:entry["reservation"]))):
                raise ValueError("search episode contains unresolved or rejected reservation")
            if semantic_digest(source.state_dict()) != model_before:
                raise ValueError("frozen source changed")
            atomic_json(args.output / "complete-report.json", dict(status="passed",training_admissible=False,
                purpose="complete-full128-engineering-not-formal-teacher",identity=identity,limits=asdict(limits),
                result=result,roots=roots,evidence=evidence,source_sha256=manifest,
                total_decisions=len(roots),total_valid_simulations=128*len(roots),model_unchanged=True,
                exclusions=["G2-qualified-teacher","empirical-prior-calibration","constant-cost-belief-rebuild",
                    "general-hidden-mechanism-noninterference","model-strength-acceptance"]))
    except BaseException as error:
        atomic_json(args.output / "continuation-failure.json", dict(status="failed",error=repr(error),
            committed_groups=len(trace.committed),tick=None if journal is None else journal.rollout.runtime.clock.tick,
            training_admissible=False))
        raise
    finally:
        if journal is not None:
            journal.close()
        factory.close()
        for native in natives:
            native.close_transport()


if __name__ == "__main__":
    main()

