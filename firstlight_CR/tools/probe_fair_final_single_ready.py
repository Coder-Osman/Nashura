import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.adapter import native_payment_mask
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


SOURCE_SHA = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"


def legal_act(information, delay):
    for card_index, cell in information.legality.plays[:, :, delay].nonzero().tolist():
        action = FullAction(information.legality.card_ids[card_index], cell, delay)
        if information.legality.legal(action):
            return action
    raise ValueError("diagnostic requires a legal continuation ACT")


def rejected_action(environment, information):
    owner = information.owner
    raw = environment.raw_observation
    player = next(player for player in raw["players"] if player["owner"] == owner)
    for card in sorted(player["hand"], key=lambda card: int(card["cost"]), reverse=True):
        payable = native_payment_mask(tick=int(raw["tick"]), elixir_raw=int(player["elixirRaw"]),
            cost_raw=int(card["cost"]) * 10000, timeline=environment.mode_timeline.timeline)
        if bool(payable[1]):
            continue
        entry = environment.action_mask(owner).placement_masks[str(card["handIndex"])]
        for row, cells in enumerate(entry["row_major"]):
            for column, valid in enumerate(cells):
                if valid:
                    cell = row * 18 + column
                    action = FullAction(int(card["cardId"]), cell if owner == 0 else 575 - cell, 1)
                    assert not information.legality.legal(action)
                    return action, dict(native_tick=int(raw["tick"]), elixir_raw=int(player["elixirRaw"]),
                        cost_raw=int(card["cost"]) * 10000, payable_100ms=False, legal=False)
    return None


def state(rollout):
    return dict(tick=rollout.runtime.clock.tick, clock=rollout.runtime.clock.capture(),
        information={str(owner): info.sha256 for owner, info in rollout.freeze_due_information().items()},
        native_digest=rollout.runtime.environment.native._request("state-digest")["sha256"],
        rng={str(owner): semantic_digest(generator.get_state()) for owner, generator in rollout.generators.items()})


def continuation(rollout, owner, mode, deadline):
    opponent = 1 - owner
    pending = rollout.runtime.clock.pending[opponent]
    information = rollout.freeze_due_information()
    assert set(information) == {owner}
    action = FullAction() if mode == "staggered_wait" else legal_act(information[owner],
        1 if mode == "early_public_act" else 29)
    assert information[owner].legality.legal(action)
    random_sample = torch.rand(8, generator=rollout.generators[owner]).tolist()
    opponent_rng = rollout.generators[opponent].get_state().clone()
    rollout.commit_simultaneous({owner: action})
    own_pending = rollout.runtime.clock.pending[owner]
    assert rollout.runtime.clock.pending[opponent] == pending
    trace = []
    target = own_pending.due_tick if mode != "staggered_wait" else pending.due_tick
    while rollout.runtime.clock.tick < target:
        deadline()
        assert rollout.runtime.clock.pending[opponent] == pending
        outcomes = rollout.advance()
        trace.append(dict(tick=rollout.runtime.clock.tick, outcomes=outcomes,
            ready=sorted(rollout.runtime.clock.ready),
            pending=[asdict(plan) for plan in rollout.runtime.clock.pending.values()],
            events=[event.to_dict() for event in rollout.runtime.environment.action_outcomes()]))
        if rollout.runtime.clock.tick < target:
            assert not rollout.freeze_due_information()
    boundaries = [state(rollout)]
    if mode == "staggered_wait":
        assert rollout.runtime.clock.ready == {opponent}
        assert rollout.runtime.clock.pending == {owner: own_pending}
        rollout.commit_simultaneous({opponent: FullAction()})
        while rollout.runtime.clock.tick < own_pending.due_tick:
            deadline()
            assert rollout.runtime.clock.pending[owner] == own_pending
            rollout.advance()
        assert rollout.runtime.clock.ready == {owner}
        boundaries.append(state(rollout))
        expected = {opponent: "wait_expired", owner: "wait_expired"}
    else:
        assert rollout.runtime.clock.ready == {0, 1} and not rollout.runtime.clock.pending
        expected = {owner: "executed", opponent: "preempted" if mode == "early_public_act" else "wait_expired"}
    actual = {entry["owner"]: entry["reason"] for entry in rollout.runtime.clock.evidence[-2:]}
    assert actual == expected
    assert torch.equal(rollout.generators[opponent].get_state(), opponent_rng)
    return dict(mode=mode, selected=asdict(action), selected_legal=True,
        random_sample=random_sample, opponent_rng_unchanged=True, preserved=asdict(pending),
        trace=trace, boundaries=boundaries, final=state(rollout))


def run_case(native, source, owner, sidecar_factory, deadline):
    environment = FinalBattleEnv(native=native)
    environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78102081 + owner,
        end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
        options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
            allow_initial_remaining_runtime_rejections=True))
    native.start_atomic_steps()
    rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78102091 + owner)
    preparation = []
    for boundary in range(32):
        deadline()
        information = rollout.freeze_due_information()
        assert set(information) == {0, 1} and not rollout.runtime.clock.pending
        rejected = rejected_action(environment, information[owner])
        if rejected is not None:
            legal_act(information[owner], 1)
            break
        action = legal_act(information[owner], 1)
        preparation.append(dict(tick=environment.current_tick, action=asdict(action), legal=True))
        rollout.commit_simultaneous({owner: action, 1 - owner: FullAction()})
        while not rollout.runtime.frozen:
            deadline()
            assert all(reason == "executed" for reason in rollout.advance().values())
    else:
        raise ValueError("no bounded rejection fixture found")
    action, payment = rejected
    start = environment.current_tick
    invalid_information = information[owner].sha256
    selected = {owner: action, 1 - owner: FullAction()}
    rollout.runtime.commit_simultaneous(selected, legal=lambda seat, choice: choice == selected[seat])
    rollout.information = {}
    opponent_plan = rollout.runtime.clock.pending[1 - owner]
    rejected_plan = rollout.runtime.clock.pending[owner]
    assert rollout.advance() == {}
    outcomes = rollout.advance()
    assert outcomes == {rejected_plan.identity: "rejected"}
    assert rollout.runtime.clock.ready == {owner}
    assert rollout.runtime.clock.pending == {1 - owner: opponent_plan}
    native_rejection = [event.to_dict() for event in environment.action_outcomes()]
    assert any(event["event_type"] == "action_rejected" for event in native_rejection)
    root = state(rollout)
    branches = []
    with rollout.snapshot(sidecar_factory) as snapshot:
        for mode in ("staggered_wait", "early_public_act", "same_tick_public_act"):
            pair = []
            for repetition in range(2):
                snapshot.restore()
                assert state(rollout) == root
                pair.append(continuation(rollout, owner, mode, deadline))
                rollout.runtime.failed = True
            assert pair[0] == pair[1], f"single-ready {mode} replay diverged"
            branches.append(dict(mode=mode, replays=pair, exact=True))
        snapshot.restore()
        assert state(rollout) == root and not rollout.runtime.failed
    assert snapshot.native.handle is None
    return dict(owner=owner, purpose="native-rejection-diagnostic", training_admissible=False,
        published=False, injected_illegal_action=asdict(action), mask_bypass=True, payment=payment,
        preparation=preparation, start_tick=start, rejected_reservation=asdict(rejected_plan),
        pre_rejection_fair_sha=invalid_information, native_outcomes=native_rejection, root=root,
        branches=branches, snapshot_released=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--puct-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    started = time.monotonic()
    def deadline():
        if time.monotonic() - started > 600:
            raise TimeoutError("single-ready diagnostic deadline")
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    manifest_path = Path(__file__).resolve().parents[1] / "probe-source-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((manifest_path.parent / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("single-ready deployed source differs")
    source, _, identity = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    source.eval()
    before = semantic_digest(source.state_dict())
    records = []
    for owner in (0, 1):
        native = create_search_native(args.port, transport="compact-delta")
        try:
            records.append(run_case(native, source, owner, BattleEnvSidecar, deadline))
            print(json.dumps(dict(owner=owner, status="passed", tick=records[-1]["root"]["tick"])), flush=True)
        finally:
            native.close_transport()
    assert semantic_digest(source.state_dict()) == before
    atomic_json(args.output, dict(schema="fair-final-native-single-ready-diagnostic.v1", status="passed",
        training_admissible=False, published=False, normal_training_single_ready_verified=False,
        wake_protocol=rollout_protocol(), checkpoint_sha256=identity, model_unchanged=True,
        source_sha256=manifest, records=records, seconds=time.monotonic() - started))
    print(json.dumps(dict(status="passed", cases=len(records), seconds=time.monotonic() - started)), flush=True)


def rollout_protocol():
    from native_runner.training.fair_final.native import FinalDecisionRuntime
    return FinalDecisionRuntime.wake_protocol


if __name__ == "__main__":
    main()
