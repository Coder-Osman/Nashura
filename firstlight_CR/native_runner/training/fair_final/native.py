from copy import deepcopy
from collections import deque
from dataclasses import replace
import time
from types import SimpleNamespace

from native_runner.battle_env import BattleEnvError, BattleEnvV1, _PendingRecord
from native_runner.contracts import ActionV1, TerminalV1, frozen_mapping
from native_runner.cr_native_env import HandAction
from native_runner.training.event_action_space import event_cell_to_native

from .timing import DecisionClock
from .visibility import event_visible_to_owner


class FinalBattleEnv(BattleEnvV1):
    timing_protocol = "fair-final-due-before-observe.v1"

    @staticmethod
    def _event_visible_to(event, owner, hidden_entity_ids=frozenset()):
        return event_visible_to_owner(event, owner)

    def __init__(self, *args, **kwargs):
        if kwargs.get("include_native_digest", False) or any(kwargs.get(name, 0) for name in
                ("shaping_beta", "terminal_margin_beta", "truncation_terminal_potential")):
            raise ValueError("final runtime requires FAIR observations and terminal-only rewards")
        super().__init__(*args, **{**kwargs, "include_native_digest": False})
        self._owner_events = {owner: deque(maxlen=self.event_limit) for owner in (0, 1)}
        self._owner_event_archive = {owner: {} for owner in (0, 1)}

    def reset(self, *args, **kwargs):
        for owner in (0, 1):
            self._owner_events[owner].clear()
            self._owner_event_archive[owner].clear()
        return super().reset(*args, **kwargs)

    def _append_event(self, event):
        for owner, events in self._owner_events.items():
            if not event_visible_to_owner(event, owner):
                continue
            if len(events) == self.event_limit:
                evicted = events[0]
                record = self._owner_event_archive[owner].setdefault(evicted.event_type,
                    dict(count=0, last_tick=evicted.tick))
                record["count"] += 1
                record["last_tick"] = max(record["last_tick"], evicted.tick)
            events.append(event)
        super()._append_event(event)

    def _events_for(self, owner, tick, hidden_entity_ids=frozenset()):
        cutoff = max(0, tick - self.event_window_ticks)
        return tuple(event for event in self._owner_events.get(owner, ()) if cutoff <= event.tick <= tick)

    def _event_summary_for(self, owner, tick, hidden_entity_ids=frozenset()):
        cutoff = max(0, tick - self.event_window_ticks)
        records = deepcopy(self._owner_event_archive.get(owner, {}))
        for event in self._owner_events.get(owner, ()):
            if event.tick < cutoff:
                record = records.setdefault(event.event_type, dict(count=0, last_tick=event.tick))
                record["count"] += 1
                record["last_tick"] = max(record["last_tick"], event.tick)
        return dict(window_ticks=self.event_window_ticks, window_ms=self.event_window_ticks * 50,
            earlier_counts={key: value["count"] for key, value in records.items()},
            earlier_last_tick={key: value["last_tick"] for key, value in records.items()})

    def observe_dual(self, *args, policy_features_only=False, **kwargs):
        observations = super().observe_dual(*args, policy_features_only=policy_features_only, **kwargs)
        result = {}
        for owner, observation in observations.items():
            events = self._events_for(owner, observation.tick)
            metadata = dict(observation.metadata)
            if not policy_features_only:
                metadata["event_history"] = frozen_mapping({**self._event_summary_for(owner, observation.tick),
                    "returned": len(events), "limit": self.event_limit})
            result[owner] = replace(observation, events=events, metadata=frozen_mapping(metadata))
        return result

    def _terminal(self, raw, towers):
        if raw.get("finalized") is not True:
            return TerminalV1()
        return super()._terminal(raw, towers)

    def advance_reservations(self, reservations):
        before = self.raw_observation
        native_tick = int(before["tick"])
        tick = self.current_tick
        if self._pending_abilities or any(item.status == "queued" for item in self._pending):
            raise BattleEnvError("final runtime owns all native commands")
        if (len({plan.owner for plan in reservations}) != len(reservations)
                or any(plan.due_tick != tick + 1 or plan.action.card_id is None for plan in reservations)):
            raise BattleEnvError("only next-tick ACT reservations may enter the native queue")
        prepared = []
        outcomes = {}
        for plan in reservations:
            player = next(player for player in before["players"] if player["owner"] == plan.owner)
            card = next((card for card in player["hand"] if int(card["cardId"]) == plan.action.card_id), None)
            if card is None:
                outcomes[plan.identity] = "rejected"
                continue
            runtime = self._hand_runtime_contract(card,
                form_aliases=self._native_form_aliases(self._rich_context(before)), owner=plan.owner)
            if int(runtime["native_form_code"]) != 0 or int(runtime["form_code"]) != 0:
                raise BattleEnvError("final v1 requires base card forms")
            slot = int(card["handIndex"])
            placement = self.action_mask(plan.owner, before).placement_masks.get(str(slot))
            if placement is None:
                raise BattleEnvError("missing placement identity for a reserved card")
            grid = event_cell_to_native(plan.action.cell, owner=plan.owner, board_width=18)
            offset = placement.get("model_subcell_offset")
            if offset is not None:
                sign = 1 if plan.owner == 0 else -1
                offset = tuple(float(value) * sign for value in offset)
            action = ActionV1.play(plan.owner, slot, grid, subcell_offset=offset,
                card_id=plan.action.card_id, execute_offset_ticks=1,
                action_id=f"{self.episode_id}:final:{plan.identity}")
            prepared.append((plan, action, runtime))
        pending = {}
        for plan, action, runtime in prepared:
            coordinates = self._world_target(action)
            receipt = self.native.queue_hand_action_at(
                HandAction(plan.owner, action.hand_slot, *coordinates), execute_tick=native_tick + 1)
            expected = {"cardId": int(runtime["visible_card_id"]),
                "commandCardId": int(runtime["native_effective_card_id"]),
                "cost": float(runtime["effective_cost"]), "formCode": int(runtime["native_form_code"]),
                "executeTick": native_tick + 1}
            if any(receipt.get(key) != value for key, value in expected.items()):
                raise BattleEnvError("native reservation receipt changed its exact execution contract")
            if receipt.get("queuedAtTick") != native_tick or receipt.get("terminalCanceled"):
                raise BattleEnvError("native reservation was not queued at its final command boundary")
            now = int(time.time() * 1000)
            record = _PendingRecord(action=action, card_id=plan.action.card_id,
                effective_card_id=int(runtime["effective_card_id"]),
                native_effective_card_id=int(runtime["native_effective_card_id"]),
                requested_native_tick=native_tick, expected_native_tick=native_tick + 1,
                hand_slot=action.hand_slot, card_parameter=int(receipt["cardParameter"]),
                deck_slot=int(receipt["deckSlot"]), cost=int(receipt["cost"]),
                form_code=int(runtime["form_code"]), native_form_code=int(runtime["native_form_code"]),
                form_name=str(receipt["formName"]), requested_at_ms=now, generated_at_ms=now, inference_end_ms=now)
            self._pending.append(record)
            pending[plan.identity] = record
        self.reset_decision_deadlines()
        result = self.step({owner: ActionV1.wait(owner, ticks=1) for owner in (0, 1)},
            record_trace=False, policy_features_only=True, defer_observations=True)
        if self.current_tick != tick + 1:
            raise BattleEnvError("native final step did not advance exactly one tick")
        for identity, record in pending.items():
            if record.status not in ("executed", "rejected"):
                raise BattleEnvError("native reservation has no final execution attestation")
            outcomes[identity] = record.status
        return outcomes, result


class FinalDecisionRuntime:
    wake_protocol = "public-deployment-own-deadline.v1"

    def __init__(self, environment):
        self.environment = environment
        self.clock = DecisionClock()
        self.failed = False
        self.ended = False
        self.truncated = False
        self.frozen = {}
        self._freeze(environment.current_tick, (0, 1), {})

    def _freeze(self, tick, owners, outcomes):
        observations = None

        def freeze(owner, current_tick):
            nonlocal observations
            if observations is None:
                observations = self.environment.observe_dual(policy_features_only=True)
            observation = observations[owner]
            if observation.tick != current_tick:
                raise BattleEnvError("frozen FAIR observation tick differs from settled boundary")
            return observation

        self.frozen = self.clock.begin_tick(tick, wake_owners=owners,
            adjudicate_due=lambda plans, current_tick: outcomes, freeze=freeze)

    def freeze_due_information(self):
        if self.failed:
            raise BattleEnvError("failed final runtime requires explicit checkpoint recovery")
        return deepcopy(self.frozen)

    def commit_simultaneous(self, actions, *, legal):
        if self.failed or self.ended or self.truncated:
            raise BattleEnvError("final runtime is not accepting decisions")
        self.clock.commit(actions, legal=legal)
        self.frozen = {}

    def advance(self):
        if self.failed or self.ended or self.truncated or self.clock.ready:
            raise BattleEnvError("advance requires a live runtime with all decisions committed")
        try:
            target = self.clock.tick + 1
            plans = tuple(plan for plan in self.clock.pending.values()
                if plan.due_tick == target and plan.action.card_id is not None)
            outcomes, result = self.environment.advance_reservations(plans)
            _, _, terminated, truncated, _ = result
            self.ended, self.truncated = bool(terminated["__all__"]), bool(truncated["__all__"])
            expected = {plan.identity for plan in plans}
            if set(outcomes) != expected:
                raise BattleEnvError("incomplete native reservation outcomes")
            public_play = any(event.event_type == "action_executed"
                and event.data.get("private_to") is None
                and all(event_visible_to_owner(event, owner) for owner in (0, 1))
                for event in self.environment.action_outcomes())
            owners = (0, 1) if public_play else ()
            if self.ended or self.truncated or not getattr(self.environment, "policy_actions_open", True):
                self.clock.finish(target, outcomes=outcomes)
                self.frozen = {}
            else:
                self._freeze(target, owners, outcomes)
            return outcomes
        except Exception:
            self.failed = True
            raise

    def snapshot(self, sidecar_factory):
        return FinalSnapshot(self, sidecar_factory)


class FinalSnapshot:
    def __init__(self, runtime, sidecar_factory):
        if runtime.failed or runtime.ended or runtime.truncated:
            raise BattleEnvError("snapshot requires a live verified runtime")
        self.runtime = runtime
        self.environment = runtime.environment
        self.native = self.environment.native
        self.sidecar = sidecar_factory(self.environment)
        self.battle_state = self.sidecar.capture()
        self.clock = runtime.clock.capture()
        self.frozen = deepcopy(runtime.frozen)
        self.match_id = self.environment.episode_id
        self.digest = self.native._request("state-digest")["sha256"]
        self.handle = self.native.create_snapshot()

    def restore(self):
        if self.handle is None or self.runtime.environment is not self.environment:
            raise BattleEnvError("snapshot is released or belongs to a different runtime")
        if self.environment.episode_id != self.match_id:
            raise BattleEnvError("snapshot belongs to a different match")
        try:
            observation = self.native.restore(self.handle)
            digest = self.native._request("state-digest")["sha256"]
            if digest != self.digest:
                raise BattleEnvError("snapshot restoration changed native state")
            native_receipt = observation["restore"]
            receipt = SimpleNamespace(generation=int(native_receipt["generation"]),
                state_epoch=int(native_receipt["stateEpoch"]), tick=int(native_receipt["tick"]), native_digest=digest)
            self.sidecar.restore(self.battle_state, receipt=receipt)
            self.environment._raw = {key: value for key, value in observation.items() if key != "restore"}
            self.environment._invalidate_rich_cache()
            from native_runner.training.fair_search.native import NativeBeliefWorld

            NativeBeliefWorld._rebase_native_cursors(self,
                self.environment._rich_snapshot_for(self.environment.raw_observation), receipt)
            self.runtime.clock = DecisionClock.restore(self.clock)
            if self.environment.current_tick != self.runtime.clock.tick:
                raise BattleEnvError("restored native clock differs from controller")
            self.runtime.frozen = deepcopy(self.frozen)
            self.runtime.failed = self.runtime.ended = self.runtime.truncated = False
        except Exception:
            self.runtime.failed = True
            raise

    def close(self):
        if self.handle is not None:
            self.native.release_snapshot(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
