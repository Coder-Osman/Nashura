from copy import deepcopy
import importlib
import json
from pathlib import Path
import random
import sys
import time
from types import SimpleNamespace

from .belief import IncompatibleBelief, NativeConditioner, known_own_draw
from .config import HOG_DECK
from .contracts import DrawHistory, FrozenHistory, InformationState, PublicEvent, semantic_digest, unpack, pack
from .draw import ConditionalDraws
from .transport import create_search_native


def install_puct(puct_root):
    root = Path(puct_root).resolve()
    if not (root / "event_policy_rollout.py").is_file():
        raise ValueError("explicit verified PUCT runtime directory is required")
    module_names = ("runtime_contract", "event_policy_rollout", "recorder_capsule", "policy_rollout",
                    "action_codec", "state_codec", "native_adapter", "tree_search")
    for name in module_names:
        cached = sys.modules.get(name)
        if cached is not None and Path(getattr(cached, "__file__", "")).resolve().parent != root:
            del sys.modules[name]
    sys.path.insert(0, str(root))
    contract = importlib.import_module("runtime_contract")
    if Path(contract.__file__).resolve().parent != root:
        raise ValueError("runtime contract provenance differs")
    contract.install()
    recorder = importlib.import_module("recorder_capsule")
    if Path(recorder.__file__).resolve().parent != root:
        raise ValueError("PUCT recorder capsule provenance differs")
    module = importlib.import_module("event_policy_rollout")
    if Path(module.__file__).resolve() != root / "event_policy_rollout.py":
        raise ValueError("PUCT module provenance differs")
    return module.EventPolicyRollout


class NativeMatchRuntime:
    def __init__(self, port, policies, owner_sha256, *, rollout_type, seed, match_id, decks=None,
                 transport="compact", execution_only=False):
        from native_runner.battle_env import BattleEnvV1

        self.native = create_search_native(port, transport=transport)
        self.seed = seed
        self.environment = BattleEnvV1(native=self.native)
        bindings = {owner: SimpleNamespace(model=model.policy, checkpoint_sha256=owner_sha256[owner],
                                          contract_sha256=owner_sha256[owner])
                    for owner, model in policies.items()}
        self.rollout = rollout_type(self.environment, bindings, reward=None, match_id=match_id)
        if self.rollout.reward_config is not None or getattr(self.rollout, "overflow", None) is not None:
            raise ValueError("FAIR runtime requires terminal-only rewards")
        self.rollout.reset(decks=dict(enumerate(decks or (HOG_DECK, HOG_DECK))), seed=seed)
        if transport in ("compact", "compact-delta"):
            self.native.start_atomic_steps()
        if self.rollout.reward_config is not None or self.rollout.overflow is not None:
            raise ValueError("FAIR reset enabled reward shaping")
        self.execution_only = execution_only
        if execution_only:
            self.rollout.begin_search_root(search_only=True)
        self.history = {owner: FrozenHistory("own") for owner in (0, 1)}
        self.public = FrozenHistory("public")
        self.public_seen = set()
        self.closed = False
        for owner, observation in self.rollout.current_policy_observations().items():
            player = next(player for player in observation.players if player.owner == owner)
            slots = player.metadata["hand_slot_by_card"]
            ordered = sorted(player.hand, key=lambda card: int(slots.get(str(card), slots.get(card))))
            if len(ordered) != 4 or player.next_card is None:
                raise ValueError("native own initial hand is incomplete")
            self.history[owner] = self.history[owner].append(
                json.dumps(dict(initial_draw=[*ordered, player.next_card]), sort_keys=True))

    @property
    def ended(self):
        return self.rollout.terminal

    def information_for(self, owner):
        rollout = self.rollout
        if owner not in rollout.ready:
            raise ValueError("owner is not at a decision")
        frame = unpack(pack(rollout.frames[owner]))
        masks = {name: value.detach().cpu().clone() for name, value in rollout.masks(owner).items()}
        return InformationState(owner, rollout.tick, frame, masks, self.public,
                                self.history[owner], deepcopy(rollout.plans[owner]))

    def freeze_due_information(self):
        return {owner: self.information_for(owner) for owner in self.rollout.ready}

    def commit_simultaneous(self, selected, frozen):
        if set(selected) != set(frozen) or set(selected) != set(self.rollout.ready):
            raise ValueError("simultaneous submission misses due owners")
        for owner, information in frozen.items():
            if information.sha256 != self.information_for(owner).sha256 or not information.legal(selected[owner]):
                raise ValueError("frozen information changed before simultaneous commit")
        for owner in tuple(self.rollout.ready):
            self.commit(owner, selected[owner])

    def commit(self, owner, action):
        if owner not in self.rollout.ready:
            raise ValueError("owner is not at a decision")
        observation = self.rollout.current_policy_observations()[owner]
        player = next(player for player in observation.players if player.owner == owner)
        record = dict(tick=self.rollout.tick, action=action, next_card=player.next_card)
        if getattr(self.rollout, "_search_execution_ledger", None) is None:
            self.rollout.decide(owner, key=action)
        else:
            self.rollout.commit_search_action(owner, action)
        history = self.history[owner]
        self.history[owner] = (history.append_decision(record, self.sampled_play_count - (4 - len(player.hand)))
                               if isinstance(history, DrawHistory) else
                               FrozenHistory.freeze(history, "own").append(json.dumps(record, sort_keys=True)))

    def advance(self, stop_tick=None, *, replay_events=()):
        next_tick = self.rollout.tick + 1
        stop_tick = next_tick if stop_tick is None else min(stop_tick, next_tick)
        original_step = self.environment.step
        had_step_override = "step" in vars(self.environment)
        if replay_events:
            from native_runner.contracts import ActionV1
            from native_runner.training.event_action_space import native_hand_slots

            def replay_step(commands, **kwargs):
                commands = dict(commands)
                observations = self.rollout.current_policy_observations()
                for event in replay_events:
                    if event.tick != self.rollout.tick + 1:
                        raise ValueError("public replay command missed its execution boundary")
                    player = next(player for player in observations[event.owner].players
                                  if player.owner == event.owner)
                    slots = {card: slot for slot, card in native_hand_slots(player)}
                    if event.card_id not in slots:
                        raise ValueError("public replay card is absent at the execution boundary")
                    grid = (event.x // 1000, event.y // 1000)
                    offset = ((event.x - grid[0] * 1000 - 500) / 1000,
                              (event.y - grid[1] * 1000 - 500) / 1000)
                    action = ActionV1.play(event.owner, slots[event.card_id], grid,
                                           subcell_offset=offset, card_id=event.card_id,
                                           execute_offset_ticks=1,
                                           action_id=f"{self.rollout.match_id}:replay:{self.rollout.ledger.submitted}")
                    self.rollout.ledger.submit(action)
                    commands[event.owner] = (action,)
                return original_step(commands, **kwargs)

            self.environment.step = replay_step
        try:
            outcomes = self.rollout.step_native(stop_tick=stop_tick)
        except ValueError as error:
            if str(error) != "observation contains a future event":
                raise
            current = self.rollout.tick
            events = getattr(self.environment, "_events", ())
            future = [(event.event_type, event.tick) for event in events if event.tick > current]
            raise ValueError(f"{error}: rollout_tick={current}, native_tick={self.native.observe().get('tick')}, "
                             f"future={future[:8]}") from error
        finally:
            if replay_events:
                if had_step_override:
                    self.environment.step = original_step
                else:
                    del self.environment.step
        self.collect_public_events(outcomes)
        return outcomes

    def collect_public_events(self, events=None):
        from native_runner.battle_env import BattleEnvV1

        if events is None:
            events = (event for observation in self.rollout.current_policy_observations().values()
                      for event in observation.events)
        added = set()
        for event in events:
            if event.event_type != "action_executed" or event.card_id not in HOG_DECK or event.position is None:
                continue
            if not any(BattleEnvV1._event_visible_to(event, owner) for owner in (0, 1)):
                continue
            position = event.position
            x, y = (position[0], position[1]) if isinstance(position, (tuple, list)) else (position.x, position.y)
            record = PublicEvent(event.owner, event.tick, event.card_id,
                                 round(x), round(y))
            if record not in self.public_seen:
                added.add(record)
        key = lambda event: (event.tick, event.owner, event.card_id, event.x, event.y)
        self.public = FrozenHistory.freeze(self.public, "public")
        for record in sorted(added, key=key):
            if self.public and key(record) < key(self.public[-1]):
                raise ValueError("public receipt arrived before the retained history boundary")
            self.public = self.public.append(record)
            self.public_seen.add(record)
            if record.owner == getattr(self, "sampled_owner", None):
                self.sampled_play_count += 1

    def terminal_evidence(self):
        if self.rollout.reward_config is not None or self.rollout.overflow is not None:
            raise ValueError("FAIR terminal evidence contains reward shaping")
        observations = self.rollout.current_policy_observations()
        terminal = observations[0].terminal
        counters = self.rollout.ledger.counters()
        if observations[1].terminal != terminal:
            raise ValueError("native owners disagree on terminal evidence")
        return dict(ended=terminal.ended, truncated=any(observation.truncated for observation in observations.values()),
                    winner=terminal.winner, terminal_tick=terminal.terminal_tick, reason=terminal.reason,
                    rejected=counters["rejected"], unconfirmed=counters["unconfirmed"])

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                if self.execution_only:
                    self.rollout.end_search_root()
            finally:
                self.native.close_transport()


class NativeBeliefWorld:
    def _capture_histories(self):
        runtime = self.runtime
        runtime.history = {owner: FrozenHistory.freeze(history, "own") for owner, history in runtime.history.items()}
        runtime.public = FrozenHistory.freeze(runtime.public, "public")
        self.saved_history = dict(runtime.history)
        self.saved_public = runtime.public
        self.saved_sampled_play_count = getattr(runtime, "sampled_play_count", 0)

    def _capture_environment(self):
        adapter = importlib.import_module("native_adapter")
        self.battle_sidecar = adapter.BattleEnvSidecar(
            self.runtime.environment, discard_epoch_cache=True,
            share_immutable_events=True, copy_on_write_mappings=True,
            share_placement_cache=True)
        self.battle_state = self.battle_sidecar.capture()

    def __init__(self, runtime, owner, *, search_only=False):
        self.runtime, self.owner = runtime, owner
        self.snapshot = None
        self.sidecar = None
        self.search_only = search_only
        self.trace = []
        self.root_info = runtime.information_for(owner)
        self.runtime.rollout.begin_search_root(search_only=search_only)
        try:
            self._capture_environment()
            self.sidecar = (runtime.rollout.capture_search() if search_only else
                            deepcopy(runtime.rollout.capture()))
            self.snapshot = runtime.native.create_snapshot()
            self.root_digest = runtime.native._request("state-digest")["sha256"]
            self._capture_histories()
        except BaseException:
            try:
                if self.snapshot is not None:
                    runtime.native.release_snapshot(self.snapshot)
                    self.snapshot = None
            finally:
                try:
                    runtime.rollout.end_search_root()
                finally:
                    runtime.close()
            raise

    def restore_root(self):
        self.runtime.native.clear_replay_schedule()
        observation = self.runtime.native.restore(self.snapshot)
        digest = self.runtime.native._request("state-digest")["sha256"]
        if digest != self.root_digest:
            raise ValueError("world-local root restore checksum differs")
        adapter = importlib.import_module("native_adapter")
        receipt = adapter.NativeRestoreReceipt.from_observation(observation, digest)
        self.battle_sidecar.restore(self.battle_state, receipt=receipt)
        environment = self.runtime.environment
        environment._raw = {key: value for key, value in observation.items() if key != "restore"}
        environment._invalidate_rich_cache()
        self._rebase_native_cursors(environment._rich_snapshot_for(environment.raw_observation), receipt)
        if self.search_only:
            self.runtime.rollout.restore_search(self.sidecar)
        else:
            self.runtime.rollout.restore(deepcopy(self.sidecar))
        current = FrozenHistory.freeze(self.runtime.public, "public")
        if not current.extends(self.saved_public):
            raise ValueError("branch public history changed the shared root prefix")
        self.runtime.public_seen.difference_update(current[len(self.saved_public):])
        self.runtime.history = dict(self.saved_history)
        self.runtime.public = self.saved_public
        self.runtime.sampled_play_count = self.saved_sampled_play_count
        if self.runtime.environment.current_tick != self.runtime.rollout.tick:
            raise ValueError("restored environment tick differs from rollout")
        if self.owner not in self.runtime.rollout.ready:
            raise ValueError("belief root owner is not ready after restore")
        self.runtime.rollout.ready.remove(self.owner)
        self.runtime.rollout.ready.insert(0, self.owner)

    def replace_root(self):
        cache = getattr(self.runtime.rollout, "_search_restore_cache", None)
        previous = cache.get(id(self.sidecar)) if cache is not None else None
        try:
            self._replace_root()
        finally:
            if cache is not None:
                current = cache.get(id(self.sidecar))
                if current is None and previous is not None and previous[0] is self.sidecar:
                    current = previous
                cache.clear()
                if current is not None:
                    cache[id(self.sidecar)] = current

    def _replace_root(self):
        runtime = self.runtime
        root_info = runtime.information_for(self.owner)
        ledger = getattr(runtime.rollout, "_search_execution_ledger", None)
        if ledger is not None:
            indices = sorted(set(ledger.current_by_owner.values()) | set(ledger.submitted.values()))
            remap = {previous: current for current, previous in enumerate(indices)}
            ledger.entries = [ledger.entries[index] for index in indices]
            ledger.current_by_owner = {owner: remap[index] for owner, index in ledger.current_by_owner.items()}
            ledger.submitted = {identity: remap[index] for identity, index in ledger.submitted.items()}
        previous_environment = self.battle_sidecar, self.battle_state
        self._capture_environment()
        next_environment = self.battle_sidecar, self.battle_state
        self.battle_sidecar, self.battle_state = previous_environment
        sidecar = (runtime.rollout.capture_search() if self.search_only else
                   deepcopy(runtime.rollout.capture()))
        snapshot = runtime.native.create_snapshot()
        try:
            root_digest = runtime.native._request("state-digest")["sha256"]
            runtime.native.release_snapshot(self.snapshot)
        except BaseException:
            runtime.native.release_snapshot(snapshot)
            raise
        self.snapshot, self.sidecar = snapshot, sidecar
        self.battle_sidecar, self.battle_state = next_environment
        self.root_info, self.root_digest = root_info, root_digest
        self._capture_histories()
        self.trace = []

    def condition_draws(self, queues, initial):
        from native_runner.training.event_cache import canonicalize_event_observation

        runtime = self.runtime
        if runtime.sampled_owner != 1 - self.owner:
            raise ValueError("conditioning requires an independently sampled belief world")
        observation = runtime.native.observe()
        players = {player["owner"]: player for player in observation["players"]}
        expected, replacement = [], []
        for owner in (0, 1):
            player = players[owner]
            slots = [-1] * 4
            for item in player["hand"]:
                slots[item["handIndex"]] = item["deckSlot"]
            slots.extend(item["deckSlot"] for item in sorted(player["cycle"], key=lambda item: item["cycleIndex"]))
            mapping = {item["cardId"]: item["deckSlot"] for item in player["deck"]}
            mapping[-1] = -1
            queue = queues[owner]
            if len(queue) != len(slots) or set(card for card in queue if card != -1) != set(HOG_DECK):
                raise ValueError("conditioned draw differs from fixed deck")
            expected.extend([*slots, *([-2] * (12 - len(slots)))])
            replacement.extend([*(mapping[card] for card in queue), *([-2] * (12 - len(queue)))])
        fields = [observation["generation"], observation["stateEpoch"], observation["tick"],
                  *expected, *replacement]
        receipt_raw = runtime.native._request("belief-draw-probe " + " ".join(map(str, fields)))
        if receipt_raw.get("ok") is not True or receipt_raw.get("tick") != runtime.rollout.tick:
            raise ValueError("conditioned draw receipt differs")
        environment = runtime.environment
        environment._raw = dict(runtime.native.observe())
        environment._invalidate_rich_cache()
        adapter = importlib.import_module("native_adapter")
        receipt = adapter.NativeRestoreReceipt.from_observation(receipt_raw, "")
        self._rebase_native_cursors(environment._rich_snapshot_for(environment.raw_observation), receipt)
        rollout = runtime.rollout
        rollout._step_observations = None
        rollout._decision_context_cache = None
        observations = rollout.current_policy_observations()
        for owner in (0, 1):
            frame = canonicalize_event_observation(rollout.tensorizers[owner].tensorize(observations[owner], validate=False))
            rollout.frames[owner] = frame
            rollout.legals[owner] = rollout._legality(owner, observations[owner], frame)
        opponent = 1 - self.owner
        runtime.history[opponent] = runtime.history[opponent].rebind(initial[opponent])
        if runtime.information_for(self.owner).sha256 != self.root_info.sha256:
            raise ValueError("conditioning changed previously available FAIR information")

    def _rebase_native_cursors(self, snapshot, receipt):
        if (snapshot.generation, snapshot.state_epoch, snapshot.tick) != (
                receipt.generation, receipt.state_epoch, receipt.tick):
            raise ValueError("restored telemetry identity differs from native receipt")
        environment = self.runtime.environment
        for kind, envelope in (("combat", snapshot.combat_events),
                               ("phase", snapshot.phase_runtime),
                               ("visibility", snapshot.visibility_runtime),
                               ("remaining", snapshot.remaining_runtime)):
            if envelope is None:
                continue
            window = envelope.window if kind == "phase" else envelope
            rejected = (envelope.rejected_capture_count if kind == "combat" else window.rejected_count)
            events = envelope.events
            valid_phase_probe = kind == "phase" and all(
                event.tick == receipt.tick and event.kind == "movement_scale"
                and event.sequence == window.oldest_retained_sequence + index
                for index, event in enumerate(events))
            if (not window.complete or window.overflow_count or rejected
                    or window.epoch_first_sequence != window.oldest_retained_sequence
                    or window.next_sequence - window.oldest_retained_sequence != len(events)
                    or (events and not valid_phase_probe)):
                raise ValueError(f"restored {kind} event ring is not a clean restore boundary")
            setattr(environment, f"_{kind}_event_identity", (receipt.generation, receipt.state_epoch))
            setattr(environment, f"_{kind}_next_sequence", window.next_sequence)
            if kind == "combat":
                environment._public_card_play_event_identity = (receipt.generation, receipt.state_epoch)
                environment._public_card_play_next_sequence = window.next_sequence
            elif kind == "remaining":
                environment._remaining_rejected_count_baseline = 0

    def root_information(self):
        return self.runtime.information_for(self.owner)

    def information(self):
        information = self.runtime.information_for(self.runtime.rollout.ready[0])
        self.trace.append(("information", information.sha256))
        return information

    def apply(self, action):
        self.trace.append(("action", self.runtime.rollout.tick, self.runtime.rollout.ready[0], action))
        self.runtime.commit(self.runtime.rollout.ready[0], action)

    def advance(self, remaining_seconds):
        stop_tick = self.runtime.rollout.tick + max(1, int(remaining_seconds / .05))
        while not self.runtime.ended and not self.runtime.rollout.ready and self.runtime.rollout.tick < stop_tick:
            self.runtime.advance(min(stop_tick, self.runtime.rollout.tick + 1))
        if not self.runtime.ended and not self.runtime.rollout.ready:
            rollout = self.runtime.rollout
            observations = rollout.current_policy_observations()
            from native_runner.training.event_cache import canonicalize_event_observation
            for owner in (self.owner, 1 - self.owner):
                frame = canonicalize_event_observation(rollout.tensorizers[owner].tensorize(observations[owner], validate=False))
                rollout.frames[owner] = frame
                rollout.legals[owner] = rollout._legality(owner, observations[owner], frame)
                rollout.ready.append(owner)

    def terminal_result(self, owner):
        if not self.runtime.ended:
            return None
        evidence = self.runtime.terminal_evidence()
        if not evidence["ended"] or evidence["truncated"]:
            raise ValueError("belief simulation ended without a native terminal result")
        return 0 if evidence["winner"] is None else 1 if evidence["winner"] == owner else -1

    def close(self):
        if self.snapshot is None:
            return
        try:
            self.restore_root()
        finally:
            try:
                self.runtime.native.release_snapshot(self.snapshot)
                self.snapshot = None
            finally:
                try:
                    self.runtime.rollout.end_search_root()
                finally:
                    self.runtime.close()


class NativeReplayFactory:
    def __init__(self, ports, policies, owner_sha256, rollout_type, *, diagnostic_directory=None,
                 search_only=False, transport="compact", conditional_draws=False):
        if not ports or len(set(ports)) != len(ports):
            raise ValueError("belief requires exclusive native ports")
        self.ports, self.policies, self.identities, self.rollout_type = tuple(ports), policies, owner_sha256, rollout_type
        self.used = set()
        self.diagnostic_directory = diagnostic_directory
        self.search_only = search_only
        self.transport = transport
        self.completed_traces = []
        if conditional_draws and transport not in ("compact", "compact-delta"):
            raise ValueError("conditional draws require resident compact transport")
        self.conditional_draws = conditional_draws
        self.conditioned_roots = 0
        self.conditioned_seconds = 0.

    def _replay(self, runtime, information, *, own_start=1, public_start=0):
        own_decisions = {entry["tick"]: entry for entry in
                         (json.loads(record) for record in information.own_history[own_start:])}
        events = [event for event in information.public_history[public_start:]
                  if event.owner != information.owner]
        cursor = 0
        while runtime.rollout.tick <= information.tick:
            tick = runtime.rollout.tick
            if tick == information.tick and information.owner in runtime.rollout.ready:
                break
            if runtime.ended:
                raise ValueError("conditional replay ended before the root")
            for owner in tuple(runtime.rollout.ready):
                if owner == information.owner:
                    entry = own_decisions.get(tick)
                    if entry is None:
                        raise ValueError("conditional replay introduced an own decision")
                    runtime.commit(owner, entry["action"])
                else:
                    runtime.commit(owner, "wait")
            stop = information.tick
            future_decisions = [when for when in own_decisions if when > tick]
            if future_decisions:
                stop = min(stop, min(future_decisions))
            replay_events = []
            while cursor < len(events):
                event = events[cursor]
                if event.tick <= tick:
                    raise ValueError("public replay missed a command tick")
                if event.tick != tick + 1:
                    stop = min(stop, event.tick - 1)
                    break
                replay_events.append(event)
                cursor += 1
            if stop <= tick:
                raise ValueError(f"replay stopped without a root decision: tick={tick}, target={information.tick}")
            runtime.advance(stop, replay_events=replay_events)
        runtime.collect_public_events()
        actual = runtime.information_for(information.owner)
        if actual.sha256 != information.sha256:
            from dataclasses import fields
            differences = [field.name for field in fields(actual.frame)
                           if semantic_digest(getattr(actual.frame, field.name)) !=
                           semantic_digest(getattr(information.frame, field.name))]
            raise ValueError("native replay does not match the complete FAIR root: "
                             f"expected={information.sha256[:12]}, actual={actual.sha256[:12]}, "
                             f"tick={actual.tick}/{information.tick}, frame_differences={differences}")

    def update(self, world, information):
        try:
            self._update(world, information)
        except Exception as error:
            if self.diagnostic_directory is not None:
                import torch
                from uuid import uuid4

                directory = Path(self.diagnostic_directory)
                directory.mkdir(parents=True, exist_ok=True)
                destination = directory / f"update-root-{information.owner}-{information.tick}-{uuid4().hex}.pt"
                temporary = destination.with_suffix(".tmp")
                pool = getattr(world, "draw_pool", None)
                draw_state = None if pool is None else dict(
                    initial={side: values.tolist() for side, values in pool.initial.items()},
                    current={side: values.tolist() for side, values in pool.current.items()},
                    public_cursor=pool.public_cursor, own_cursor=pool.own_cursor,
                    play_counts=pool.play_counts, next_refill=pool.next_refill)
                try:
                    torch.save(dict(schema="fair-update-diagnostic.v1", error=str(error),
                                    information=information.to_payload(),
                                    previous=world.root_information().to_payload(),
                                    seed=getattr(world, "draw_seed", None), draw_state=draw_state), temporary)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
            raise

    def _update(self, world, information):
        previous = world.root_information()
        if (previous.owner != information.owner or information.tick < previous.tick
                or not information.own_history.extends(previous.own_history)
                or not information.public_history.extends(previous.public_history)):
            if self.conditional_draws:
                raise ValueError("conditional root does not extend the cached FAIR history")
            raise IncompatibleBelief("incremental root does not extend the cached FAIR history")
        if previous.sha256 == information.sha256:
            return
        proposed = None
        if self.conditional_draws:
            from native_runner.training.v4.factory import production_semantic_bundle
            proposed, queues = world.draw_pool.propose(
                information, production_semantic_bundle().card_catalog,
                rng=random.Random(semantic_digest((world.draw_seed, information.sha256))))
        world.restore_root()
        conditioned = False
        try:
            self._replay(world.runtime, information, own_start=len(previous.own_history),
                         public_start=len(previous.public_history))
        except ValueError as error:
            if not self.conditional_draws:
                raise IncompatibleBelief(str(error)) from error
            world.restore_root()
            conditioning_started = time.perf_counter()
            try:
                world.condition_draws(queues, proposed.selected_initial)
                self._replay(world.runtime, information, own_start=len(previous.own_history),
                             public_start=len(previous.public_history))
            except BaseException:
                world.restore_root()
                raise
            self.conditioned_roots += 1
            self.conditioned_seconds += time.perf_counter() - conditioning_started
            conditioned = True
        self.completed_traces.append(semantic_digest(world.trace))
        world.replace_root()
        if proposed is not None:
            world.draw_pool = proposed
            if conditioned:
                world.draw_initial = proposed.selected_initial

    def __call__(self, information, *, seed, ordinal):
        from native_runner.match_factory import MatchConfig, tournament_level_kwargs
        from native_runner.training.v4.factory import production_semantic_bundle

        available = [port for port in self.ports if port not in self.used]
        if not available:
            raise ValueError("exclusive belief world capacity exhausted")
        port = available[0]
        own_draw = known_own_draw(information, production_semantic_bundle().card_catalog)
        temporary = create_search_native(port, transport=self.transport)
        try:
            conditioner = NativeConditioner(temporary, lambda deck0, deck1, match_seed:
                MatchConfig(deck0=deck0, deck1=deck1, seed=match_seed, **tournament_level_kwargs()))
            required = {}
            public_sequences = {}
            for event in information.public_history:
                if event.owner != information.owner:
                    required.setdefault(event.owner, event.card_id)
                    public_sequences.setdefault(event.owner, []).append(event.card_id)
            decks, _ = conditioner.create(own_draw,
                                          seed=seed, rng=random.Random(seed),
                                          required_initial_cards=required,
                                          public_play_sequences=public_sequences)
        finally:
            temporary.close_transport()
        runtime = NativeMatchRuntime(port, self.policies, self.identities, rollout_type=self.rollout_type,
                                     seed=seed, match_id=f"belief-{information.owner}-{ordinal}", decks=decks,
                                     transport=self.transport)
        runtime.history[information.owner] = FrozenHistory.freeze((information.own_history[0],), "own")
        try:
            if self.conditional_draws:
                opponent = 1 - information.owner
                runtime.sampled_owner, runtime.sampled_play_count = opponent, 0
                runtime.history[opponent] = DrawHistory(tuple(json.loads(runtime.history[opponent][0])["initial_draw"]))
                pool = ConditionalDraws(information.owner, json.loads(information.own_history[0])["initial_draw"])
                pool, _ = pool.propose(information, production_semantic_bundle().card_catalog, rng=random.Random(seed))
                from .belief import initial_draw
                initial = {player["owner"]: initial_draw(player)[0] for player in runtime.native.observe()["players"]}
            self._replay(runtime, information)
            world = NativeBeliefWorld(runtime, information.owner, search_only=self.search_only)
            if self.conditional_draws:
                world.draw_pool, world.draw_seed = pool, seed
                world.draw_initial = initial
            self.used.add(port)
            original_close = world.close

            def close():
                try:
                    self.completed_traces.append(semantic_digest(world.trace))
                    original_close()
                finally:
                    self.used.discard(port)

            world.close = close
            return world
        except BaseException as error:
            try:
                if self.diagnostic_directory is not None:
                    import torch
                    directory = Path(self.diagnostic_directory)
                    directory.mkdir(parents=True, exist_ok=True)
                    torch.save(dict(information=information.to_payload(), seed=seed, ordinal=ordinal,
                                    error=str(error)),
                           directory / f"replay-root-{information.owner}-{information.tick}-{ordinal}.pt")
                    if "actual" in locals():
                        torch.save(actual.to_payload(), directory / "actual-root.pt")
            finally:
                runtime.close()
            raise
