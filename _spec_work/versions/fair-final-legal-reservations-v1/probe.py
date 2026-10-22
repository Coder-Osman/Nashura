import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
ROOT_TICKS = (90, 110, 2598, 5602)


def choices(information):
    selected = []
    for card_index, card_id in enumerate(information.legality.card_ids):
        for delay in (1, 30):
            cells = information.legality.plays[card_index, :, delay].nonzero().flatten().tolist()
            if cells:
                action = FullAction(card_id, cells[0] if delay == 1 else cells[-1], delay)
                if not information.legality.legal(action):
                    raise ValueError("enumerated reservation is outside the complete legal mask")
                selected.append(action)
    return selected


def identity(rollout):
    return dict(tick=rollout.runtime.clock.tick, clock=rollout.runtime.clock.capture(),
        fair={str(owner): info.sha256 for owner, info in rollout.freeze_due_information().items()},
        native_sha=rollout.runtime.environment.native._request("state-digest")["sha256"],
        rng={str(owner): semantic_digest(generator.get_state()) for owner, generator in rollout.generators.items()})


def trials(information):
    owner_choices = {owner: choices(info) for owner, info in information.items()}
    for owner in (0, 1):
        for action in owner_choices[owner]:
            yield dict(kind="single-act", actions={owner: action, 1 - owner: FullAction()})
    for delay in (1, 30):
        candidates = {owner: [action for action in actions if action.delay_slot == delay]
            for owner, actions in owner_choices.items()}
        if all(candidates.values()):
            yield dict(kind="simultaneous-act", actions={owner: actions[0] for owner, actions in candidates.items()})
        cannon = {owner: next((index for index, card in enumerate(info.legality.card_ids)
            if card == 27000000), None) for owner, info in information.items()}
        if all(index is not None for index in cannon.values()):
            shared = [cell for cell in range(576)
                if information[0].legality.plays[cannon[0], cell, delay]
                and information[1].legality.plays[cannon[1], 575 - cell, delay]]
            if shared:
                yield dict(kind="mirrored-cell-cannon", actions={0: FullAction(27000000, shared[0], delay),
                    1: FullAction(27000000, 575 - shared[0], delay)})


def branch(rollout, actions, deadline):
    information = rollout.freeze_due_information()
    if set(actions) != set(information) or not all(information[owner].legality.legal(action)
            for owner, action in actions.items()):
        raise ValueError("probe attempted an illegal or incomplete simultaneous decision")
    start = rollout.runtime.clock.tick
    evidence_start = len(rollout.runtime.clock.evidence)
    rollout.commit_simultaneous(actions)
    reservations = [asdict(plan) for plan in rollout.runtime.clock.pending.values()]
    steps = []
    while not rollout.runtime.frozen and not rollout.runtime.ended and not rollout.runtime.truncated:
        deadline()
        if rollout.runtime.clock.tick - start >= 60:
            raise ValueError("legal reservation skipped its maximum decision deadline")
        outcomes = rollout.advance()
        steps.append(dict(tick=rollout.runtime.clock.tick, outcomes=outcomes,
            events=[event.to_dict() for event in rollout.runtime.environment.action_outcomes()]))
    if rollout.runtime.truncated or rollout.runtime.failed:
        raise ValueError("legal reservation branch was truncated or failed")
    evidence = rollout.runtime.clock.evidence[evidence_start:]
    return dict(actions={str(owner): asdict(action) for owner, action in sorted(actions.items())},
        selected_legal=True, mask_bypass=False, reservations=reservations, steps=steps,
        evidence=evidence, rejected=any(row["reason"] == "rejected" for row in evidence),
        single_ready=len(rollout.runtime.clock.ready) == 1, final=identity(rollout))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    for name in ("source", "trace", "puct-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()

    def deadline():
        if time.monotonic() - started > 1200:
            raise TimeoutError("bounded legal reservation probe deadline")

    torch.set_num_threads(1)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    project = Path(__file__).resolve().parent
    manifest = json.loads((project / "probe-source-manifest.json").read_text())
    for relative, digest in manifest.items():
        if hashlib.sha256((project / relative).read_bytes()).hexdigest() != digest:
            raise ValueError("legal reservation probe source identity differs")
    trace = json.loads(args.trace.read_text())
    groups = {group["tick"]: group for group in trace["boundaries"]}
    if not set(ROOT_TICKS) <= groups.keys():
        raise ValueError("registered legal probe roots are absent from the fixed trajectory")
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    model_before = semantic_digest(source.state_dict())
    native = create_search_native(args.port, transport="compact-delta")
    records, replayed = [], []
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101906,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101946)
        for target in ROOT_TICKS:
            while environment.current_tick < target:
                deadline()
                information = rollout.freeze_due_information()
                if information:
                    group = groups[environment.current_tick]
                    if {str(owner): info.sha256 for owner, info in information.items()} != group["fair_sha"]:
                        raise ValueError("registered trajectory FAIR digest differs")
                    actions = {int(owner): FullAction(**action) for owner, action in group["actions"].items()}
                    rollout.commit_simultaneous(actions)
                    replayed.append(dict(tick=environment.current_tick, legal=True))
                if any(reason != "executed" for reason in rollout.advance().values()):
                    raise ValueError("registered trajectory contains a rejected reservation")
                if rollout.runtime.ended or rollout.runtime.truncated:
                    raise ValueError("registered trajectory ended before the diagnostic root")
            information = rollout.freeze_due_information()
            if {str(owner): info.sha256 for owner, info in information.items()} != groups[target]["fair_sha"]:
                raise ValueError("diagnostic root FAIR digest differs")
            root = identity(rollout)
            cases = []
            with rollout.snapshot(BattleEnvSidecar) as snapshot:
                for trial in trials(information):
                    repetitions = []
                    for repetition in range(2):
                        snapshot.restore()
                        if identity(rollout) != root:
                            raise ValueError("legal probe snapshot did not restore its root")
                        repetitions.append(branch(rollout, trial["actions"], deadline))
                    if repetitions[0] != repetitions[1]:
                        raise ValueError("legal reservation continuation changed after snapshot restore")
                    cases.append(dict(kind=trial["kind"], repetitions=repetitions, exact=True))
                snapshot.restore()
                if identity(rollout) != root:
                    raise ValueError("legal probe final restore differs")
            if snapshot.native.handle is not None:
                raise ValueError("legal probe snapshot handle leaked")
            masks = {str(owner): dict(card_ids=list(info.legality.card_ids),
                shape=list(info.legality.plays.shape),
                plays_hex=bytes(info.legality.plays.flatten().tolist()).hex())
                for owner, info in information.items()}
            records.append(dict(tick=target, root=root, masks=masks, trials=cases, snapshot_released=True))
            atomic_json(args.output.with_name("progress.json"), dict(tick=target,
                trials=sum(len(record["trials"]) for record in records), seconds=time.monotonic() - started))
            print(json.dumps(dict(tick=target, trials=len(cases), status="verified")), flush=True)
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("legal probe changed the frozen policy")
        atomic_json(args.output, dict(schema="fair-final-legal-reservations.v1", status="completed",
            training_admissible=False, source_sha=source_sha, source_files=manifest,
            trace_sha=hashlib.sha256(args.trace.read_bytes()).hexdigest(), mask_bypass=False,
            records=records, replayed=replayed, model_unchanged=True,
            limitations=["one-fixed-trajectory", "bounded-candidate-subset", "no-global-unreachability-proof",
                "no-belief-reconstruction-or-Full128-at-single-ready-root"],
            seconds=time.monotonic() - started))
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
