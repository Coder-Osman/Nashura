import argparse
import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace


def inspect(source, scheduled_tick):
    parsed = ast.parse(source.read_text())
    rollout = next(node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == 'EventPolicyRollout')
    methods = [node for node in rollout.body if isinstance(node, ast.FunctionDef)
               and node.name in ('step_native', '_due_commands')]
    if len(methods) != 2:
        raise ValueError('runtime audit method set changed')
    extracted = ast.Module(body=methods, type_ignores=[])
    observations = {0: SimpleNamespace(tick=101), 1: SimpleNamespace(tick=101)}
    commands = []

    def step(actions, **kwargs):
        commands.append(actions)
        environment.current_tick = 101
        return observations, {0: 0., 1: 0.}, {'__all__': False}, {'__all__': False}, {}

    environment = SimpleNamespace(current_tick=100, step=step, action_outcomes=lambda: (),
        deployment_timing_events=lambda owner: [dict(nativeEventId='fixture-visible-event', kind='card_play')]
        if owner == 1 else [], observe_dual=lambda **kwargs: observations)
    namespace = dict(policy_actions_open=lambda *args: True, gameplay_end_tick=lambda *args: 10000,
        wait_span=lambda *args: 1, complete_event_commands=lambda actions: actions,
        ActionV1=SimpleNamespace(wait=lambda owner, ticks: dict(kind='wait', owner=owner, ticks=ticks)),
        canonicalize_event_observation=lambda frame: frame)
    exec(compile(extracted, str(source), 'exec'), namespace)
    driver = SimpleNamespace(terminal=False, ready=[], tick=100, environment=environment,
        lazy_step_observations=True, plans={0: dict(scheduled_tick=scheduled_tick), 1: None},
        recovery_owners=set(), event_ticks=[], last_decision_by_owner={0: 99, 1: 99},
        max_advance_ticks=60, config=SimpleNamespace(max_wait_ticks=60), overflow=None,
        step_timing_profile=None, steps=0, ledger=SimpleNamespace(reconcile=lambda outcomes: (), ability_events=[]),
        _search_execution_ledger=None, recorder=SimpleNamespace(reconcile=lambda outcomes: None, advance=lambda **kwargs: None),
        totals={0: 0., 1: 0.}, seen_events=set(), native_events=0, learner_owner=0,
        tensorizers={owner: SimpleNamespace(tensorize=lambda observation, **kwargs: observation) for owner in (0, 1)},
        frames={}, legals={}, _count_profile=lambda *args: None,
        _legality=lambda *args: SimpleNamespace(decision_ready=True))
    driver._due_commands = lambda observations: namespace['_due_commands'](driver, observations)
    namespace['step_native'](driver)
    return dict(start_tick=100, event_tick=101, scheduled_tick=scheduled_tick,
        ready_owners=driver.ready, old_plan_at_observation=driver.plans[0],
        submitted_commands=commands,
        passed=0 not in driver.ready or driver.plans[0] is None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    checks = {name: inspect(args.source, scheduled) for name, scheduled in
              (('same_tick_due_before_decision', 101), ('earlier_event_resolves_plan_before_observation', 102))}
    report = dict(gate_id='G0.engine-order', source=str(args.source),
        source_sha256=hashlib.sha256(args.source.read_bytes()).hexdigest(),
        method='execute deployed Python step_native and _due_commands AST with a deterministic native-event fixture',
        scope='controller event ordering; native engine integration remains NOT RUN',
        passed=all(row['passed'] for row in checks.values()), checks=checks)
    args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
