import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

import torch

from legal_probe import SOURCE_SHA, branch, identity
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.event_action_space import event_cell_to_native
from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.rollout import FinalRollout
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.fair_search.model import load_checkpoint
from native_runner.training.fair_search.transport import create_search_native


CANNON = 27000000
HOG = 26000021
FIREBALL = 28000000


def nearest_action(information, card_id, target, delay=1):
    if card_id not in information.legality.card_ids:
        return None
    index = information.legality.card_ids.index(card_id)
    cells = information.legality.plays[index, :, delay].nonzero().flatten().tolist()
    if not cells:
        return None
    cell = min(cells, key=lambda candidate: ((candidate % 18 - target[0]) ** 2
        + (candidate // 18 - target[1]) ** 2, candidate))
    return FullAction(card_id, cell, delay)


def trajectory_action(information, *, pocket_open):
    owner = information.owner
    cannon_present = nearest_action(information, CANNON, (3, 10), delay=30) is not None
    if owner == 1 and cannon_present:
        return FullAction()
    if owner == 0 and not pocket_open:
        for card_id, target in ((HOG, (3, 14)), (FIREBALL, (3, 25))):
            action = nearest_action(information, card_id, target)
            if action is not None:
                return action
    if pocket_open and cannon_present:
        return FullAction()
    for card_id in (26000010, 26000030, 28000011, 26000038, 26000000):
        action = nearest_action(information, card_id, (3, 1))
        if action is not None:
            return action
    return FullAction()


def collision_actions(information, delay):
    if set(information) != {0, 1}:
        return None
    cells = {}
    for owner, info in information.items():
        if CANNON not in info.legality.card_ids:
            return None
        index = info.legality.card_ids.index(CANNON)
        cells[owner] = info.legality.plays[index, :, delay].nonzero().flatten().tolist()
    for left in cells[0]:
        left_grid = event_cell_to_native(left, owner=0, board_width=18)
        for right in cells[1]:
            right_grid = event_cell_to_native(right, owner=1, board_width=18)
            if abs(left_grid[0] - right_grid[0]) < 3 and abs(left_grid[1] - right_grid[1]) < 3:
                return {0: FullAction(CANNON, left, delay), 1: FullAction(CANNON, right, delay)}
    return None


def mask_record(information):
    return {str(owner): dict(card_ids=list(info.legality.card_ids), shape=list(info.legality.plays.shape),
        plays_hex=bytes(info.legality.plays.flatten().tolist()).hex()) for owner, info in information.items()}


def public_towers(towers):
    return [dict(owner=tower.owner, kind=tower.tower_kind, hp=tower.hitpoints,
        active=tower.active, position=tower.position) for tower in towers]


def replay_setup(native, catalog, trajectory, root, deadline):
    environment = FinalBattleEnv(native=native)
    environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101907,
        end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
        options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
            allow_initial_remaining_runtime_rejections=True))
    native.start_atomic_steps()
    rollout = FinalRollout(environment, catalog=catalog, execution_seed=78101947)
    cursor = 0
    while environment.current_tick < root["tick"]:
        deadline()
        information = rollout.freeze_due_information()
        if information:
            if cursor >= len(trajectory):
                raise ValueError("replay produced an extra decision boundary")
            row = trajectory[cursor]
            if environment.current_tick != row["tick"] or {
                    str(owner): info.sha256 for owner, info in information.items()} != row["fair_sha"]:
                raise ValueError("pocket setup replay FAIR boundary differs")
            observations = rollout.runtime.freeze_due_information()
            if public_towers(next(iter(observations.values())).towers) != row["public_towers"]:
                raise ValueError("replayed public towers differ")
            rollout.commit_simultaneous({int(owner): FullAction(**action)
                for owner, action in row["actions"].items()})
            cursor += 1
        if any(status != "executed" for status in rollout.advance().values()):
            raise ValueError("replayed setup reservation rejected")
        if rollout.runtime.ended or rollout.runtime.truncated:
            raise ValueError("replayed setup ended early")
    replayed_root = identity(rollout)
    if cursor != len(trajectory) or any(replayed_root[key] != root[key] for key in ("tick", "clock", "fair", "rng")):
        raise ValueError("fresh reset did not reconstruct the collision information state")
    return dict(status="passed", boundaries=cursor, root=replayed_root,
        public_towers=public_towers(next(iter(rollout.runtime.freeze_due_information().values())).towers),
        native_digest_compared=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    for name in ("source", "trace", "puct-root", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()

    def deadline():
        if time.monotonic() - started > 1200:
            raise TimeoutError("bounded pocket collision deadline")

    torch.set_num_threads(1)
    sys.path.insert(0, str(args.puct_root))
    from native_adapter import BattleEnvSidecar

    project = Path(__file__).resolve().parent
    manifest = json.loads((project / "probe-source-manifest.json").read_text())
    for relative, digest in manifest.items():
        if hashlib.sha256((project / relative).read_bytes()).hexdigest() != digest:
            raise ValueError("pocket probe source differs")
    source, _, source_sha = load_checkpoint(args.source, device="cpu", expected_sha256=SOURCE_SHA)
    model_before = semantic_digest(source.state_dict())
    native = create_search_native(args.port, transport="compact-delta")
    trajectory, records = [], []
    try:
        environment = FinalBattleEnv(native=native)
        environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK, seed=78101907,
            end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
            options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                allow_initial_remaining_runtime_rejections=True))
        native.start_atomic_steps()
        rollout = FinalRollout(environment, catalog=source.policy.backbone.catalog, execution_seed=78101947)
        pocket_open = False
        while environment.current_tick < 3500 and not rollout.runtime.ended and not rollout.runtime.truncated:
            deadline()
            information = rollout.freeze_due_information()
            if information:
                observations = rollout.runtime.freeze_due_information()
                towers = next(iter(observations.values())).towers
                pocket_open = pocket_open or any(tower.owner == 1 and tower.tower_kind != "king"
                    and (not tower.active or tower.hitpoints == 0) for tower in towers)
                actions = collision_actions(information, 1) if pocket_open else None
                if actions is not None:
                    root = identity(rollout)
                    root_towers = public_towers(towers)
                    root_masks = mask_record(information)
                    with rollout.snapshot(BattleEnvSidecar) as snapshot:
                        for delay in (1, 30):
                            selected = collision_actions(information, delay)
                            if selected is None:
                                continue
                            repetitions = []
                            for repetition in range(2):
                                snapshot.restore()
                                if identity(rollout) != root:
                                    raise ValueError("pocket root restoration differs")
                                repetitions.append(branch(rollout, selected, deadline))
                            if repetitions[0] != repetitions[1]:
                                raise ValueError("pocket collision continuation differs")
                            records.append(dict(delay=delay, repetitions=repetitions, exact=True))
                        snapshot.restore()
                        if identity(rollout) != root:
                            raise ValueError("pocket final restore differs")
                    if snapshot.native.handle is not None:
                        raise ValueError("pocket snapshot leaked")
                    break
                actions = {owner: trajectory_action(info, pocket_open=pocket_open)
                    for owner, info in information.items()}
                trajectory.append(dict(tick=environment.current_tick, pocket_open=pocket_open,
                    fair_sha={str(owner): info.sha256 for owner, info in information.items()},
                    actions={str(owner): asdict(action) for owner, action in actions.items()},
                    selected_legal=all(information[owner].legality.legal(action) for owner, action in actions.items()),
                    public_towers=public_towers(towers)))
                rollout.commit_simultaneous(actions)
            outcomes = rollout.advance()
            if any(status != "executed" for status in outcomes.values()):
                raise ValueError("pocket setup trajectory reservation rejected")
            if information:
                atomic_json(args.output.with_name("progress.json"), dict(tick=environment.current_tick,
                    decisions=len(trajectory), pocket_open=pocket_open, seconds=time.monotonic() - started))
        if semantic_digest(source.state_dict()) != model_before:
            raise ValueError("pocket probe changed frozen model")
        tick, ended, truncated = environment.current_tick, rollout.runtime.ended, rollout.runtime.truncated
        native.close_transport()
        native = create_search_native(args.port, transport="compact-delta")
        replay = replay_setup(native, source.policy.backbone.catalog, trajectory, root, deadline) if records else None
        if records and replay["public_towers"] != root_towers:
            raise ValueError("collision root public towers differ after fresh reset")
        atomic_json(args.output, dict(schema="fair-final-pocket-collision.v1",
            status="collision-tested" if records else "bounded-no-collision-root",
            training_admissible=False, mask_bypass=False, source_sha=source_sha,
            source_files=manifest, seed=78101907, execution_seed=78101947, model_unchanged=True,
            tick=tick, ended=ended, truncated=truncated,
            pocket_open=pocket_open, trajectory=trajectory, records=records,
            root=root if records else None, masks=root_masks if records else None,
            root_public_towers=root_towers if records else None, replay=replay,
            snapshot_released=bool(records), seconds=time.monotonic() - started))
        print(json.dumps(dict(status="completed", tick=environment.current_tick,
            pocket_open=pocket_open, trials=len(records))), flush=True)
    finally:
        native.close_transport()


if __name__ == "__main__":
    main()
