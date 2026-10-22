"""Dual-owner V4 policy rollout state for the Hog tree-search experiment.

The native snapshot adapter owns the engine handle.  This module owns the
mutable policy-side state that must travel with that handle when a PUCT branch
is restored.  It deliberately keeps model input unchanged: deck identity is
only used by the existing tensorizer legality contract.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, is_dataclass
from enum import Enum
import math
import time
from importlib import import_module
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent / "runtime"))

from native_runner.contracts import ActionV1, ObservationV1
from native_runner.training.v4.policy_session import PolicyDecisionV4, PolicySessionV4
from native_runner.training.v4.tensors import RecurrentPolicyStateV4


SCHEMA = "hog-dual-policy-rollout.v1"
_EPISODE_FIELDS = (
    "_opponent_revealed",
    "_opponent_evolution_revealed",
    "_ability_state_by_source_entity",
    "_previous_action",
    "_episode_id",
    "_episode_start_tick",
    "_last_tensorized_tick",
)
_TRACKER_FIELDS = (
    "tick",
    "elixir",
    "elixir_bounds",
    "_observing_owner",
    "_card_states",
    "_seen_events",
    "_revealed_card_order",
    "_ability_activated_tick",
    "_ability_activated_tick_by_source",
    "_ability_card_by_source",
    "_ability_charge_state_by_source",
    "_ability_activation_identities",
    "_ability_exact_spend_identities",
    "_ready",
)


def _clone(value: Any) -> Any:
    """Clone tensors without retaining an inference-mode storage alias."""

    if isinstance(value, torch.Tensor):
        result = value.detach().cpu().clone()
        return result
    if is_dataclass(value) and not isinstance(value, type):
        return type(value)(**{field.name: _clone(getattr(value, field.name)) for field in fields(value)})
    if isinstance(value, Mapping):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_clone(item) for item in value)
    if isinstance(value, list):
        return [_clone(item) for item in value]
    if isinstance(value, set):
        return {_clone(item) for item in value}
    return deepcopy(value)


def _clone_seen_events(value: Any) -> set[tuple[object, ...]] | dict[object, object]:
    if isinstance(value, dict) and not value:
        return {}
    if not isinstance(value, set) or any(
        not isinstance(key, tuple) or any(type(part) not in (int, str, type(None)) for part in key)
        for key in value
    ):
        raise ValueError("public tracker event identities must be immutable scalar tuples")
    return value.copy()


def _tensor_state_to_cpu(state: RecurrentPolicyStateV4 | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {"hidden": _clone(state.hidden), "cell": _clone(state.cell)}


def _tensor_state_from_cpu(raw: Mapping[str, Any], device: torch.device) -> RecurrentPolicyStateV4:
    if set(raw) != {"hidden", "cell"}:
        raise ValueError("policy recurrent state fields do not match the rollout contract")
    hidden = raw["hidden"]
    cell = raw["cell"]
    if not isinstance(hidden, torch.Tensor) or not isinstance(cell, torch.Tensor):
        raise ValueError("policy recurrent state must contain tensors")
    return RecurrentPolicyStateV4(hidden=hidden.to(device=device).clone(), cell=cell.to(device=device).clone())


def _tensorizer_snapshot(tensorizer: Any, *, profile_sink=None) -> dict[str, Any]:
    state = {}
    for name in _EPISODE_FIELDS:
        started = time.perf_counter() if profile_sink is not None else 0.0
        state[name] = _clone(getattr(tensorizer, name))
        if profile_sink is not None:
            profile_sink(f"tensorizer_capture_episode_{name}_ms", started)
    tracker = getattr(tensorizer, "tracker", None)
    if tracker is not None:
        state["tracker"] = {}
        for name in _TRACKER_FIELDS:
            started = time.perf_counter() if profile_sink is not None else 0.0
            value = getattr(tracker, name)
            state["tracker"][name] = (_clone_seen_events(value) if name == "_seen_events"
                                      else _clone(value))
            if profile_sink is not None:
                profile_sink(f"tensorizer_capture_tracker_{name}_ms", started)
    else:
        state["tracker"] = None
    return state


def _restore_tensorizer(tensorizer: Any, state: Mapping[str, Any], *, profile_sink=None) -> None:
    expected = set(_EPISODE_FIELDS) | {"tracker"}
    if set(state) != expected:
        raise ValueError("tensorizer policy state fields do not match the rollout contract")
    for name in _EPISODE_FIELDS:
        started = time.perf_counter() if profile_sink is not None else 0.0
        setattr(tensorizer, name, _clone(state[name]))
        if profile_sink is not None:
            profile_sink(f"tensorizer_restore_episode_{name}_ms", started)
    tracker = getattr(tensorizer, "tracker", None)
    tracker_state = state["tracker"]
    if tracker is None:
        if tracker_state is not None:
            raise ValueError("rollout state contains tracker data for an untracked tensorizer")
        return
    if not isinstance(tracker_state, Mapping) or set(tracker_state) != set(_TRACKER_FIELDS):
        raise ValueError("tracker state fields do not match the rollout contract")
    for name in _TRACKER_FIELDS:
        started = time.perf_counter() if profile_sink is not None else 0.0
        value = tracker_state[name]
        if name == "_seen_events":
            restored = _clone_seen_events(value)
        elif name == "_card_states":
            restored = {owner: dict(cards) for owner, cards in value.items()}
        elif name == "_revealed_card_order":
            restored = {owner: list(cards) for owner, cards in value.items()}
        elif name in {
            "_ability_state_by_source_entity",
            "_ability_activated_tick_by_source",
            "_ability_card_by_source",
            "_ability_charge_state_by_source",
        }:
            restored = dict(value)
        elif name in {"_ability_activation_identities", "_ability_exact_spend_identities"}:
            restored = set(value)
        else:
            restored = _clone(value)
        setattr(tracker, name, restored)
        if profile_sink is not None:
            profile_sink(f"tensorizer_restore_tracker_{name}_ms", started)


def _action_to_dict(action: ActionV1) -> dict[str, Any]:
    return action.to_dict()


def _action_from_dict(raw: Mapping[str, Any]) -> ActionV1:
    return ActionV1.from_mapping(raw)


from state_codec import _type_name, _resolve_type, _encode, _torch_dtype, _decode


class DualPolicyRollout:
    """Keep both V4 policy sides synchronized with one native episode.

    ``capture_policy_state`` and ``restore_policy_state`` are designed to be
    passed directly to ``NativeSnapshotSearchAdapter``.  The environment's
    native snapshot and BattleEnv sidecar remain owned by that adapter.
    """

    def __init__(
        self,
        environment: Any,
        sessions: Mapping[int, PolicySessionV4],
        *,
        external_queue: Sequence[Mapping[str, Any]] = (),
        event_cursor: Mapping[str, Any] | None = None,
        reward_ledger: Mapping[str, Any] | None = None,
    ) -> None:
        if set(sessions) != {0, 1}:
            raise ValueError("dual policy rollout requires sessions for both owners")
        self.environment = environment
        self.sessions = {0: sessions[0], 1: sessions[1]}
        self.external_queue: list[dict[str, Any]] = [dict(item) for item in external_queue]
        self.event_cursor: dict[str, Any] = dict(event_cursor or {})
        self.reward_ledger: dict[str, Any] = dict(reward_ledger or {})
        self.pending_actions: dict[int, tuple[ActionV1, ...]] = {0: (), 1: ()}
        self._episode_started = False

    def start_episode(self, observations: Mapping[int, ObservationV1], *, initial_elixir: Mapping[int, float]) -> None:
        if set(observations) != {0, 1}:
            raise ValueError("dual rollout start requires both FAIR observations")
        for owner in (0, 1):
            self.sessions[owner].start_episode(observations[owner], initial_elixir=initial_elixir)
        self._episode_started = True

    def end_episode(self) -> None:
        for session in self.sessions.values():
            session.end_episode()
        self.pending_actions = {0: (), 1: ()}
        self._episode_started = False

    def decide(self, owner: int, observation: ObservationV1) -> PolicyDecisionV4:
        if owner not in (0, 1):
            raise ValueError("policy owner must be 0 or 1")
        if not self._episode_started:
            raise RuntimeError("dual rollout episode has not started")
        decision = self.sessions[owner].decide(observation)
        # Both sides may share one CUDA graph whose outputs are overwritten
        # on replay. Each side must own its recurrent output immediately.
        state = self.sessions[owner].state
        if state is not None:
            self.sessions[owner].state = RecurrentPolicyStateV4(
                hidden=state.hidden.clone(), cell=state.cell.clone())
        self.pending_actions[owner] = tuple(decision.decoded.actions)
        return decision

    def set_actions(self, owner: int, actions: Sequence[ActionV1]) -> None:
        if owner not in (0, 1):
            raise ValueError("policy owner must be 0 or 1")
        if any(action.owner != owner for action in actions):
            raise ValueError("pending action owner does not match rollout side")
        self.pending_actions[owner] = tuple(actions)

    def actions(self) -> dict[int, tuple[ActionV1, ...]]:
        return {owner: tuple(actions) for owner, actions in self.pending_actions.items()}

    def capture_policy_state(self) -> dict[str, Any]:
        if not self._episode_started:
            raise RuntimeError("cannot capture a rollout before start_episode")
        return {
            "schema": SCHEMA,
            "episode_started": True,
            "torch_rng": torch.get_rng_state().clone(),
            "cuda_rng": {
                str(device): torch.cuda.get_rng_state(device).cpu().clone()
                for device in sorted({str(session.device) for session in self.sessions.values()
                                      if session.device.type == "cuda"})
            },
            "sessions": {
                str(owner): {
                    "state": _tensor_state_to_cpu(self.sessions[owner].state),
                    "episode_started": bool(self.sessions[owner]._episode_started),
                    "first_decision": bool(self.sessions[owner]._first_decision),
                    "tensorizer": _tensorizer_snapshot(self.sessions[owner].tensorizer),
                }
                for owner in (0, 1)
            },
            "pending_actions": {
                str(owner): [_action_to_dict(action) for action in self.pending_actions[owner]]
                for owner in (0, 1)
            },
            "external_queue": _clone(self.external_queue),
            "event_cursor": _clone(self.event_cursor),
            "reward_ledger": _clone(self.reward_ledger),
        }

    def capture_search_policy_state(self) -> dict[str, Any]:
        """Return JSON-safe state for ``SearchState.policy_state``."""

        return _encode(self.capture_policy_state())

    def restore_policy_state(self, state: Mapping[str, Any]) -> None:
        state = _decode(state)
        if not isinstance(state, Mapping):
            raise ValueError("decoded dual policy rollout state is not a mapping")
        if state.get("schema") != SCHEMA or state.get("episode_started") is not True:
            raise ValueError("unsupported dual policy rollout state")
        if not isinstance(state.get("torch_rng"), torch.Tensor) or not isinstance(state.get("cuda_rng"), Mapping):
            raise ValueError("rollout random generator state is missing")
        sessions = state.get("sessions")
        if not isinstance(sessions, Mapping) or set(sessions) != {"0", "1"}:
            raise ValueError("dual rollout state must contain both policy sessions")
        for owner in (0, 1):
            saved = sessions[str(owner)]
            if not isinstance(saved, Mapping):
                raise ValueError("policy session state is malformed")
            recurrent = saved.get("state")
            self.sessions[owner].state = (
                None if recurrent is None else _tensor_state_from_cpu(recurrent, self.sessions[owner].device)
            )
            self.sessions[owner]._episode_started = bool(saved.get("episode_started"))
            self.sessions[owner]._first_decision = bool(saved.get("first_decision"))
            tensorizer = saved.get("tensorizer")
            if not isinstance(tensorizer, Mapping):
                raise ValueError("policy session tensorizer state is malformed")
            _restore_tensorizer(self.sessions[owner].tensorizer, tensorizer)
        pending = state.get("pending_actions")
        if not isinstance(pending, Mapping) or set(pending) != {"0", "1"}:
            raise ValueError("dual rollout pending action state is malformed")
        self.pending_actions = {
            owner: tuple(_action_from_dict(item) for item in pending[str(owner)])
            for owner in (0, 1)
        }
        self.external_queue = [dict(item) for item in state.get("external_queue", ())]
        self.event_cursor = dict(state.get("event_cursor", {}))
        self.reward_ledger = dict(state.get("reward_ledger", {}))
        torch.set_rng_state(state["torch_rng"])
        for device, rng_state in state["cuda_rng"].items():
            torch.cuda.set_rng_state(rng_state, device)
        self._episode_started = True

    def search_state_policy(self) -> dict[str, Any]:
        return self.capture_search_policy_state()

    def restore_search_policy(self, state: Mapping[str, Any]) -> None:
        self.restore_policy_state(state)


__all__ = ["DualPolicyRollout", "SCHEMA"]
