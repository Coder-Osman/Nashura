from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2]/"firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.policy import FullAction
from native_runner.training.fair_final.timing import DecisionClock
from native_runner.training.fair_final.native import FinalDecisionRuntime


def first_boundary(first, second, *, public_tick=None, rejected_owner=None, public_success=True):
    clock = DecisionClock()
    clock.begin_tick(90, wake_owners=(0, 1), adjudicate_due=lambda *unused:{}, freeze=lambda owner,tick:None)
    clock.commit({0:first, 1:second}, legal=lambda *unused:True)
    for tick in range(91, 151):
        due = [plan for plan in clock.pending.values() if plan.due_tick == tick and plan.action.card_id is not None]
        outcomes = {plan.identity:"rejected" if plan.owner == rejected_owner else "executed" for plan in due}
        public = tick == public_tick or public_success and any(reason == "executed" for reason in outcomes.values())
        clock.begin_tick(tick, wake_owners=(0,1) if public else (),
            adjudicate_due=lambda plans,current:outcomes, freeze=lambda owner,current:None)
        if clock.ready:
            return clock
    raise AssertionError("WAIT must expire within 60 ticks")


def enumerate_closure():
    actions = [FullAction(), *(FullAction(11, 0, delay) for delay in range(1,31))]
    histogram = Counter()
    count = 0
    for first in actions:
        for second in actions:
            for public_tick in (None, *range(91,151)):
                clock = first_boundary(first, second, public_tick=public_tick)
                if clock.ready != {0,1} or clock.pending or len(clock.evidence) != 2:
                    raise ValueError("synchronous boundary closure failed")
                histogram[clock.tick-90] += 1
                count += 1
    rejection = first_boundary(FullAction(11,0,1), FullAction(), rejected_owner=0)
    hidden_success = first_boundary(FullAction(11,0,1), FullAction(), public_success=False)
    if rejection.ready != {0} or hidden_success.ready != {0}:
        raise ValueError("assumption counterexamples missing")
    return dict(schema="fair-final-boundary-closure.v1", status="passed", training_admissible=False,
        wake_protocol=FinalDecisionRuntime.wake_protocol, cases=count, action_pairs=len(actions)**2,
        public_wake_schedules=61, duration_histogram=dict(sorted(histogram.items())),
        assumptions=["both-ready-opening", "atomic-both-owner-submission", "WAIT-60-ticks",
            "ACT-even-2-through-60-ticks", "successful-ACT-publicly-wakes-both", "no-rejected-or-unconfirmed-action"],
        counterexamples=dict(rejection_single_ready=True, nonpublic_success_single_ready=True),
        conclusion="every-nonterminal-reachable-decision-group-is-both-ready-under-listed-assumptions",
        exclusions=["native-event-publicity-without-evidence", "general-pending-world-conditioning",
            "other-wake-protocol", "search-branches-with-rejections", "training-admission"])


if __name__ == "__main__":
    report = enumerate_closure()
    report["source_sha256"] = {name:hashlib.sha256((SOURCE/name).read_bytes()).hexdigest() for name in
        ("native_runner/training/fair_final/timing.py", "native_runner/training/fair_final/native.py",
         "native_runner/training/fair_final/policy.py")}
    report["enumerator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (ROOT/"boundary-closure-report.json").write_text(json.dumps(report,indent=2))
    print(json.dumps(dict(status=report["status"],cases=report["cases"])))
