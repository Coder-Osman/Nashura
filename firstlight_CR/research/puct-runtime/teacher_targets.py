"""Real frozen-response targets and complete native episode settlement."""
from __future__ import annotations

import math
import time

from tree_search import OpponentResponse, aggregate_opponent_values, q_target


def reply_targets(evidence):
    expected, robust = aggregate_opponent_values([
        OpponentResponse(key, probability, evidence["reply_values"][key])
        for key, probability in evidence["reply_probabilities"].items()
    ])
    return expected, robust, q_target(expected, robust)


def terminal_evidence(rollout, *, native_state_sha256=None):
    """Read an actual match result; reaching a driver timeout is never a draw."""
    observations = rollout.environment.observe_dual(policy_features_only=True)
    terminal = observations[0].terminal
    if (not rollout.terminal or not terminal.ended or
            any(observation.truncated for observation in observations.values()) or
            observations[1].terminal != terminal or rollout.recorder.open_rows):
        raise ValueError("episode has no complete, untruncated native settlement")
    results = list(terminal.result_by_owner)
    recorded = rollout.recorder.results()
    if any(not math.isclose(recorded[owner], results[owner], abs_tol=1e-6) for owner in (0, 1)):
        raise ValueError("terminal reward ledger differs from native settlement")
    execution = rollout.ledger.counters()
    if execution["rejected"] or execution["unconfirmed"]:
        raise ValueError(f"episode execution ledger is not clean: {execution}")
    if native_state_sha256 is None:
        native_state_sha256 = rollout.environment.native._request("state-digest")["sha256"]
    if not isinstance(native_state_sha256, str) or len(native_state_sha256) != 64:
        raise ValueError("native terminal state digest is invalid")
    return {
        "version": "native-terminal-evidence.v1",
        "episode_id": rollout.match_id,
        "ended": True,
        "truncated": False,
        "winner": terminal.winner,
        "terminal_tick": terminal.terminal_tick,
        "reason": terminal.reason,
        "result_by_owner": results,
        "native_state_sha256": native_state_sha256,
    }


def finish_episode(rollout, *, first_action=None, max_seconds=3600.0, progress=None):
    """Execute a selected teacher action, then sample the frozen policies to a result."""
    started, last_report = time.monotonic(), 0.0
    if first_action is not None:
        if not rollout.ready:
            raise ValueError("teacher root is not restored at episode continuation")
        rollout.decide(rollout.ready[0], key=first_action)
    while not rollout.terminal:
        elapsed = time.monotonic() - started
        if elapsed >= max_seconds:
            raise TimeoutError("episode deadline reached before native settlement")
        if progress is not None and elapsed - last_report >= 30:
            progress(dict(phase="episode", tick=rollout.tick, elapsed_seconds=elapsed,
                          decisions=rollout.decisions, native_steps=rollout.steps))
            last_report = elapsed
        if rollout.ready:
            rollout.decide(rollout.ready[0], sample=True)
        else:
            rollout.step_native()
    evidence = terminal_evidence(rollout)
    if progress is not None:
        progress(dict(phase="settled", **evidence))
    return evidence
