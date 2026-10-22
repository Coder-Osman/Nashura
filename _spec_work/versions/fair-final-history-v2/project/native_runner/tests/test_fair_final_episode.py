import json
from types import SimpleNamespace

import pytest
import torch

from native_runner.contracts import TerminalV1
from native_runner.training.fair_final.episode import AcceptanceRecorder, terminal_result, validate_attempt
from native_runner.training.fair_final import episode as episode_module
from native_runner.training.fair_final.native import FinalBattleEnv, FinalDecisionRuntime
from native_runner.training.fair_final.policy import FullAction, LegalPrefixes
from native_runner.training.fair_final.rollout import DecisionInformation
from native_runner.training.fair_final.timing import DecisionClock
from native_runner.training.fair_search.storage import save_episode
from native_runner.tests.test_fair_final_runtime import TickEnvironment


IDENTITY = dict(checkpoint_sha256="a" * 64, engine_sha256="b" * 64,
    source_manifest_sha256="c" * 64, controller="test", adapter="test")


def final(winner=0):
    results = (0., 0.) if winner is None else ((1., -1.) if winner == 0 else (-1., 1.))
    terminal = TerminalV1(ended=True, winner=winner, result_by_owner=results, terminal_tick=102)
    raw = dict(finalized=True, worldResult=-1 if winner is None else winner,
        worldResultRaw=-1 if winner is None else winner, winner=winner,
        crownsRaw=[0, 0] if winner is None else [int(winner == 0), int(winner == 1)])
    return terminal, raw


def recorded(root, attempt=1):
    recorder = AcceptanceRecorder(root, match_id="native-test", attempt_id=attempt, identity=IDENTITY)
    clock = DecisionClock()
    clock.begin_tick(100, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
    legality = LegalPrefixes([11], torch.ones(1, 1, 31, dtype=torch.bool))
    information = {owner: DecisionInformation(owner, 100, {"owner": owner}, legality) for owner in (0, 1)}
    actions = {0: FullAction(11, 0, 1), 1: FullAction()}
    clock.commit(actions, legal=lambda owner, action: legality.legal(action))
    recorder.record_committed(information, actions, clock)
    runtime = SimpleNamespace(clock=clock, ended=True, truncated=False, failed=False, frozen={})
    return recorder, runtime


@pytest.mark.parametrize("winner,labels", [(0, ["WIN", "LOSS"]), (1, ["LOSS", "WIN"]), (None, ["DRAW", "DRAW"])])
def test_native_final_labels_are_player_relative(winner, labels):
    terminal, raw = final(winner)
    assert terminal_result(terminal, raw, truncated=False, failed=False)["labels"] == labels


@pytest.mark.parametrize("invalid", ["truncated", "failed", "unfinished", "not_finalized", "unknown", "winner", "crowns"])
def test_missing_or_conflicting_terminal_evidence_is_never_a_draw(invalid):
    terminal, raw = final(None)
    if invalid == "unfinished":
        terminal = TerminalV1()
    if invalid == "not_finalized":
        raw["finalized"] = False
    if invalid == "unknown":
        raw["worldResult"] = 9
    if invalid == "winner":
        raw["winner"] = 1
    if invalid == "crowns":
        raw["crownsRaw"] = [0, 1]
    with pytest.raises(ValueError):
        terminal_result(terminal, raw, truncated=invalid == "truncated", failed=invalid == "failed")


def test_complete_publish_roundtrip_and_match_deduplication(tmp_path):
    for attempt in (1, 2):
        recorder, runtime = recorded(tmp_path, attempt)
        if attempt == 1:
            assert not (tmp_path / "completed/native-test.json").exists()
        runtime.clock.finish(102, outcomes={0: "executed"})
        terminal, raw = final()
        receipt = recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
        assert receipt["status"] == ("published" if attempt == 1 else "duplicate")
        episode = validate_attempt(recorder.directory)
        assert [row["wdl"] for row in episode["rows"]] == ["WIN", "LOSS"]
        assert not episode["training_admissible"]
        assert all(row["policy_target"] is None for row in episode["rows"])
    assert len(list((tmp_path / "completed").glob("*.json"))) == 1


def test_normal_preemption_retains_both_rows(tmp_path):
    recorder, runtime = recorded(tmp_path)
    runtime.clock.begin_tick(101, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
    recorder.reconcile(runtime.clock)
    assert len(recorder.rows) == 2
    assert all(row["outcome"]["reason"] == "preempted" for row in recorder.rows)


@pytest.mark.parametrize("invalid", ["rejected", "unconfirmed", "truncated"])
def test_failed_episode_is_preserved_but_never_published(tmp_path, invalid):
    recorder, runtime = recorded(tmp_path)
    if invalid != "unconfirmed":
        runtime.clock.finish(102, outcomes={0: "rejected" if invalid == "rejected" else "executed"})
    runtime.truncated = invalid == "truncated"
    terminal, raw = final()
    with pytest.raises(ValueError):
        recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    recorder.quarantine(invalid)
    assert not (tmp_path / "completed/native-test.json").exists()
    assert json.loads((recorder.directory / "quarantine.json").read_text())["published"] is False


def test_serialized_frame_corruption_is_rejected(tmp_path):
    recorder, runtime = recorded(tmp_path)
    runtime.clock.finish(102, outcomes={0: "executed"})
    terminal, raw = final()
    recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    (recorder.directory / "frames/000000.pt.gz").write_bytes(b"broken")
    with pytest.raises(ValueError, match="checksum"):
        validate_attempt(recorder.directory)


def test_final_runtime_waits_for_native_finalization_without_new_decisions():
    environment = TickEnvironment(terminal_tick=104)
    environment.policy_actions_open = True
    runtime = FinalDecisionRuntime(environment)
    runtime.commit_simultaneous({0: FullAction(), 1: FullAction()}, legal=lambda *args: True)
    environment.policy_actions_open = False
    for expected in (101, 102, 103, 104):
        runtime.advance()
        assert runtime.clock.tick == expected and not runtime.frozen and not runtime.clock.pending
    assert runtime.ended
    assert len(runtime.clock.evidence) == 2
    assert all(event["reason"] == "terminal_expired" for event in runtime.clock.evidence)


def test_dead_king_without_native_result_is_not_complete():
    environment = object.__new__(FinalBattleEnv)
    assert not environment._terminal({"ended": True, "finalized": False}, []).ended


def test_interrupted_publication_retries_without_partial_completion(tmp_path, monkeypatch):
    recorder, runtime = recorded(tmp_path)
    runtime.clock.finish(102, outcomes={0: "executed"})
    terminal, raw = final()
    original = episode_module.os.link

    def interrupted(*args):
        raise OSError("interrupted before completion marker")

    monkeypatch.setattr(episode_module.os, "link", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    assert not (tmp_path / "completed/native-test.json").exists()
    monkeypatch.setattr(episode_module.os, "link", original)
    assert recorder.publish(terminal=terminal, raw=raw, runtime=runtime)["status"] == "published"


@pytest.mark.parametrize("field", ["labels", "outcome", "policy"])
def test_altered_terminal_or_lifecycle_is_rejected_on_read(tmp_path, field):
    recorder, runtime = recorded(tmp_path)
    runtime.clock.finish(102, outcomes={0: "executed"})
    terminal, raw = final()
    recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    path = recorder.directory / "episode.json"
    payload = json.loads(path.read_text())
    if field == "labels":
        payload["result"]["labels"] = ["DRAW", "DRAW"]
    elif field == "outcome":
        payload["rows"][0]["outcome"]["tick"] = 999
    else:
        payload["rows"][0]["policy_target"] = {"wait": 128}
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        validate_attempt(recorder.directory)


def test_acceptance_episode_is_rejected_by_old_search_storage(tmp_path):
    recorder, runtime = recorded(tmp_path)
    runtime.clock.finish(102, outcomes={0: "executed"})
    terminal, raw = final()
    recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    episode = validate_attempt(recorder.directory)
    with pytest.raises(TypeError, match="SearchEpisode"):
        save_episode(tmp_path / "legacy", episode)


def test_preempted_rows_keep_terminal_supervision_after_next_boundary(tmp_path):
    recorder, runtime = recorded(tmp_path)
    clock = runtime.clock
    clock.begin_tick(101, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
    recorder.reconcile(clock)
    legality = LegalPrefixes([11], torch.ones(1, 1, 31, dtype=torch.bool))
    information = {owner: DecisionInformation(owner, 101, {"owner": owner}, legality) for owner in (0, 1)}
    actions = {0: FullAction(), 1: FullAction()}
    clock.commit(actions, legal=lambda *args: True)
    recorder.record_committed(information, actions, clock)
    clock.finish(102, outcomes={})
    terminal, raw = final()
    recorder.publish(terminal=terminal, raw=raw, runtime=runtime)
    rows = validate_attempt(recorder.directory)["rows"]
    assert len(rows) == 4
    assert [row["wdl"] for row in rows] == ["WIN", "LOSS", "WIN", "LOSS"]
    assert [row["outcome"]["reason"] for row in rows] == ["preempted", "preempted", "terminal_expired", "terminal_expired"]


def test_partial_same_tick_submission_never_creates_rows(tmp_path):
    recorder = AcceptanceRecorder(tmp_path, match_id="partial", attempt_id=1, identity=IDENTITY)
    clock = DecisionClock()
    clock.begin_tick(100, wake_owners=(0, 1), adjudicate_due=lambda *args: {}, freeze=lambda *args: None)
    with pytest.raises(ValueError, match="atomically"):
        clock.commit({0: FullAction()}, legal=lambda *args: True)
    assert not recorder.rows and not list((recorder.directory / "frames").iterdir())
