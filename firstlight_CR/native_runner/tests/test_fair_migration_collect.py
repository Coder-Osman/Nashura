from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_fair_final_episode import final
from native_runner.tests.test_fair_migration_data import context
from native_runner.tests.test_training_v4_model import _batch
from native_runner.training.fair_final.policy import LegalPrefixes
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_final.timing import DecisionClock
from native_runner.training.fair_migration_collect import collect_migration_episode, collection_protocols
from native_runner.training.fair_migration_data import MigrationStore
from native_runner.training.fair_search.contracts import semantic_digest


class ScenarioRollout:
    def __init__(self, environment, **kwargs):
        self.environment = environment
        clock = DecisionClock()
        clock.begin_tick(100, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
        self.runtime = SimpleNamespace(clock=clock, ended=False, truncated=False, failed=False, frozen={})

    def freeze_due_information(self):
        clock = self.runtime.clock
        plays = torch.zeros(8, 576, 31, dtype=torch.bool)
        return {owner: DecisionInformation(owner, clock.tick, self.environment.frames[owner],
            LegalPrefixes(self.environment.cards, plays)) for owner in sorted(clock.ready)}

    def commit_simultaneous(self, actions):
        assert set(actions) == {0, 1}
        assert set(self.runtime.clock.ready) == {0, 1}
        self.runtime.clock.commit(actions, legal=lambda *args: True)
        self.environment.committed.append(self.runtime.clock.tick)

    def advance(self):
        clock = self.runtime.clock
        if clock.tick == 100:
            clock.begin_tick(101, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
        else:
            clock.finish(102, outcomes={})
            self.runtime.ended = True
            if self.environment.bad == "rejected":
                clock.evidence[-1]["reason"] = "rejected"
            self.runtime.failed = self.environment.bad == "failed"
            self.runtime.truncated = self.environment.bad == "truncated"


def scenario(context, bad=None):
    teacher, bundle, manifest = context
    terminal, raw = final()
    environment = SimpleNamespace(cards=manifest["card_ids"], frames={owner: _batch(1) for owner in (0, 1)},
        bad=bad, raw_observation=raw, committed=[])
    environment.observe_dual = lambda **kwargs: {owner: SimpleNamespace(truncated=bad == "truncated",
        terminal=replace(terminal, winner=1) if bad == "views" and owner == 1 else terminal) for owner in (0, 1)}
    return environment


def run(store, context, environment, **kwargs):
    return collect_migration_episode(environment, teacher=context[0], store=store,
        match_id=semantic_digest("train"), attempt_id=1, execution_seed=73,
        rollout_factory=ScenarioRollout, **kwargs)


def test_collector_runs_both_sides_to_complete_terminal_and_keeps_preemptions(tmp_path, context):
    teacher, bundle, manifest = context
    store = MigrationStore(tmp_path, manifest=manifest)
    environment = scenario(context)
    progress = []
    before = semantic_digest(teacher.state_dict())
    report = run(store, context, environment, progress=progress.append)
    assert report["status"] == "passed" and report["rows"] == 4
    assert report["rows_by_owner"] == {"0": 2, "1": 2}
    assert report["lifecycle"] == {"preempted": 2, "terminal_expired": 2}
    assert environment.committed == [100, 101]
    assert progress[-1]["status"] == "passed"
    assert before == semantic_digest(teacher.state_dict()) == report["source_network_sha"]
    assert report["training_admissible"] is False


@pytest.mark.parametrize("bad", ["rejected", "failed", "truncated", "views", "not_finalized"])
def test_collector_quarantines_bad_complete_game_instead_of_publishing(tmp_path, context, bad):
    store = MigrationStore(tmp_path, manifest=context[2])
    environment = scenario(context, bad)
    if bad == "not_finalized":
        environment.raw_observation["finalized"] = False
    progress = []
    with pytest.raises(ValueError):
        run(store, context, environment, progress=progress.append)
    assert not list((store.root / "completed").glob("*.json"))
    assert json.loads((store.root / "attempts" / semantic_digest("train") / "1/quarantine.json").read_text())[
        "published"] is False
    assert progress[-1]["status"] == "failed"


def test_collector_timeout_is_quarantined_with_no_partial_completion(tmp_path, context):
    store = MigrationStore(tmp_path, manifest=context[2])
    counter = iter(range(100))
    with pytest.raises(TimeoutError):
        run(store, context, scenario(context), wall_seconds=.5, monotonic=lambda: float(next(counter)))
    assert not list((store.root / "completed").glob("*.json"))
    assert (store.root / "attempts" / semantic_digest("train") / "1/quarantine.json").exists()


@pytest.mark.parametrize("seconds", [0., -1., float("nan"), float("inf"), True])
def test_collector_rejects_invalid_budget_before_creating_attempt(tmp_path, context, seconds):
    store = MigrationStore(tmp_path, manifest=context[2])
    with pytest.raises(ValueError, match="finite migration wall"):
        run(store, context, scenario(context), wall_seconds=seconds)
    assert not (store.root / "attempts").exists()


def test_protocol_identity_binds_engine_to_real_source_and_stable_semantics():
    original = collection_protocols("a" * 64)
    changed = collection_protocols("b" * 64)
    assert original == collection_protocols("a" * 64)
    assert original["engine_protocol_sha"] != changed["engine_protocol_sha"]
    assert len(original) == 4
    assert all(value == changed[key] for key, value in original.items() if key != "engine_protocol_sha")
