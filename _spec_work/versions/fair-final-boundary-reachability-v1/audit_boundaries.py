import argparse
from collections import Counter
import hashlib
from itertools import product
import json
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent / "fair-final-history-v2/project"
sys.path.insert(0, str(PROJECT))

from native_runner.training.fair_final.native import FinalDecisionRuntime
from native_runner.training.fair_final.policy import FullAction


class OutcomeEnvironment:
    def __init__(self, outcomes):
        self.current_tick = 100
        self.outcomes = outcomes
        self.settled = []
        self.events = ()

    def observe_dual(self, **kwargs):
        return {owner: SimpleNamespace(owner=owner, tick=self.current_tick,
            settled=tuple(self.settled)) for owner in (0, 1)}

    def advance_reservations(self, reservations):
        self.current_tick += 1
        outcomes, events = {}, []
        for plan in reservations:
            outcome = self.outcomes[plan.owner]
            outcomes[plan.identity] = outcome
            self.settled.append((plan.owner, outcome, self.current_tick))
            events.append(SimpleNamespace(event_type="action_" + outcome,
                data={} if outcome == "executed" else {"private_to": plan.owner}))
        self.events = tuple(events)
        return outcomes, ({}, {}, {"__all__": False}, {"__all__": False}, {})

    def action_outcomes(self):
        return self.events


def durations():
    return (60, *range(2, 61, 2))


def cases():
    for choices in product(range(31), repeat=2):
        elapsed = min(durations()[choice] for choice in choices)
        due_acts = [owner for owner, choice in enumerate(choices)
            if choice and durations()[choice] == elapsed]
        for values in product(("executed", "rejected"), repeat=len(due_acts)):
            yield choices, dict(zip(due_acts, values))


def run_case(choices, outcomes, *, runtime_class=FinalDecisionRuntime):
    delays = [durations()[choice] for choice in choices]
    elapsed = min(delays)
    due = {owner for owner, delay in enumerate(delays) if delay == elapsed}
    if set(outcomes) != {owner for owner in due if choices[owner]}:
        raise ValueError("outcome assignment differs from earliest due ACTs")
    environment = OutcomeEnvironment(outcomes)
    runtime = runtime_class(environment)
    actions = {owner: FullAction(11, 0, choice) if choice else FullAction()
        for owner, choice in enumerate(choices)}
    runtime.commit_simultaneous(actions, legal=lambda owner, action: True)
    for offset in range(1, elapsed + 1):
        runtime.advance()
        if offset < elapsed and (runtime.clock.ready or runtime.freeze_due_information()):
            raise ValueError("boundary opened before earliest deadline")
    expected_ready = {0, 1} if "executed" in outcomes.values() else due
    expected_reasons = {owner: outcomes[owner] if choices[owner] else "wait_expired"
        for owner in due}
    expected_reasons.update({owner: "preempted" for owner in expected_ready - due})
    expected_pending = {owner: 100 + delays[owner] for owner in {0, 1} - expected_ready}
    frozen = runtime.freeze_due_information()
    if (runtime.clock.ready != expected_ready or set(frozen) != expected_ready
            or {owner: plan.due_tick for owner, plan in runtime.clock.pending.items()} != expected_pending
            or {entry["owner"]: entry["reason"] for entry in runtime.clock.evidence} != expected_reasons
            or any(entry["tick"] != 100 + elapsed for entry in runtime.clock.evidence)
            or any(observation.tick != 100 + elapsed
                or observation.settled != tuple(environment.settled) for observation in frozen.values())):
        raise ValueError("runtime differs from earliest-deadline boundary formula")
    return dict(choices=list(choices), outcomes=outcomes, elapsed_ticks=elapsed,
        ready=sorted(expected_ready), pending=expected_pending, reasons=expected_reasons)


def audit(*, runtime_class=FinalDecisionRuntime):
    if runtime_class.wake_protocol != "public-deployment-own-deadline.v1":
        raise ValueError("wake protocol differs from the conditional proof")
    counts, digest = Counter(), hashlib.sha256()
    counterexamples = {}
    for choices, outcomes in cases():
        row = run_case(choices, outcomes, runtime_class=runtime_class)
        digest.update((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode())
        counts["cases"] += 1
        no_rejections = "rejected" not in outcomes.values()
        counts["all_success_cases"] += int(no_rejections)
        counts["single_ready_cases"] += int(len(row["ready"]) == 1)
        counts["synchronous_cases"] += int(len(row["ready"]) == 2)
        if no_rejections and (row["ready"] != [0, 1] or row["pending"]):
            raise ValueError("all-success synchronization invariant failed")
        if len(row["ready"]) == 1:
            if "rejected" not in outcomes.values():
                raise ValueError("single-ready boundary lacks earliest rejection")
            counterexamples.setdefault(str(row["ready"][0]), row)
    if counts["all_success_cases"] != 31 ** 2 or set(counterexamples) != {"0", "1"}:
        raise ValueError("boundary enumeration coverage differs")
    source_paths = [ROOT / "audit_boundaries.py", ROOT / "CONTRACT.md",
        *(PROJECT / "native_runner/training/fair_final" / name
            for name in ("native.py", "timing.py", "policy.py", "visibility.py"))]
    return dict(schema="fair-final-boundary-reachability-audit.v1",
        status="verified_conditional_runtime_matrix", counts=dict(counts),
        matrix_sha256=digest.hexdigest(), rejection_counterexamples=counterexamples,
        source_sha256={str(path.relative_to(ROOT.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_paths},
        outcome_model="synthetic-public-success-private-rejection",
        legal_mask_or_native_executor_tested=False, native_rejection_reachability_proven=False,
        native_rejection_unreachability_proven=False, native_fallback_verified=False,
        training_admissible=False, gates_closed=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    with args.output.open("x") as handle:
        handle.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=report["status"], counts=report["counts"],
        matrix_sha256=report["matrix_sha256"])))
