import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("headless_native", Path(__file__).parents[1] / "headless_native.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.fixture(autouse=True)
def isolate_fake_audit(tmp_path, monkeypatch):
    original = FakeEngine.__init__

    def initialize(self, **flags):
        original(self, **flags)
        self.output = tmp_path

    monkeypatch.setattr(FakeEngine, "__init__", initialize)


def fixture():
    return {"schema": "headless-native-fixture.v1", "match": {"cmd": [], "evt": []},
            "actions": [{"tick": 90, "command": {"ct": 86, "c": {
                "t2": 90, "idHi": 0, "idLo": 1, "sel": {"os": 26000021, "pd": 99}}}}]}


class FakeEngine:
    def __init__(self, **flags):
        self.flags = flags
        self.tick = 0
        self.executed = False
        self.output = Path("/tmp")

    def stat(self):
        return {"cpu_seconds": self.tick / 100, "peak_rss_kib": 12000}

    def request(self, command):
        if command.startswith("configure "):
            self.tick = 0
            self.executed = False
            return {"ok": True}
        if command.startswith("inject "):
            self.executed = True
            return {"ok": True}
        if command.startswith("step "):
            if not self.flags.get("stalled"):
                self.tick = min(100, self.tick + int(command.split()[1]))
            return {"ok": True, "tick": self.tick, "ended": self.tick == 100}
        ended = self.tick == 100
        ordinary = {"tick": self.tick, "ended": ended,
                    "finalized": ended and not self.flags.get("unfinalized"),
                    "queuedCommands": 0, "winner": 0,
                    "players": [{"owner": 0, "accountIdHigh": 0, "accountIdLow": 1,
                                 "elixirRaw": 100000, "hand": [{"commandCardId": 26000021,
                                  "cardParameter": 99, "cost": 4}]}]}
        events = []
        if ended and self.executed and not self.flags.get("missing_receipt"):
            events.append({"sequence": 1, "kind": "card_play", "tick": 90,
                           "deploymentContext": {"owner": 0, "playedCardGlobalId": 26000021,
                                                 "cardParameter": 99}})
        return {"ok": True, "tick": self.tick, "ordinary": ordinary,
                "rich": {"combatEvents": {"events": events,
                                           "epochFirstSequence": 1,
                                           "droppedCount": int(bool(self.flags.get("overflow")))}}}


def test_canonical_removes_only_explicit_epoch_fields():
    assert MODULE.object_sha({"generation": 2, "stateEpoch": 4, "hand": [1, 2]}) == MODULE.object_sha(
        {"generation": 9, "stateEpoch": 10, "hand": [1, 2]})
    assert MODULE.object_sha({"hand": [1, 2]}) != MODULE.object_sha({"hand": [2, 1]})


def test_sequence_rebasing_preserves_relative_order_and_causes():
    def capture(base, cause_offset=0):
        return {"rich": {"combatEvents": {"epochFirstSequence": base,
                "nextSequence": base + 3, "events": [
                    {"sequence": base + 2, "causeSequence": base + cause_offset}]}}}

    assert MODULE.semantic_capture(capture(1), {}) == MODULE.semantic_capture(capture(7), {})
    assert MODULE.semantic_capture(capture(1), {}) != MODULE.semantic_capture(capture(7, 1), {})


@pytest.mark.parametrize("tick", [True, -1, 20, 90.5])
def test_fixture_rejects_invalid_tick(tick):
    value = fixture()
    value["actions"][0]["tick"] = tick
    with pytest.raises(ValueError):
        MODULE.validate_fixture(value)


def test_fixture_rejects_preloaded_commands():
    value = fixture()
    value["match"]["cmd"] = [{"hidden": True}]
    with pytest.raises(ValueError):
        MODULE.validate_fixture(value)


def test_identical_repeated_matches_reset():
    engine = FakeEngine()
    first = MODULE.play(engine, fixture())
    second = MODULE.play(engine, fixture())
    assert first["trace_sha256"] == second["trace_sha256"]
    assert first["terminal_sha256"] == second["terminal_sha256"]
    assert first["ticks"] == 100
    assert first["actions"] == 1


def test_segmented_stepping_keeps_boundary_trajectory():
    first = MODULE.play(FakeEngine(), fixture(), chunk=20)
    second = MODULE.play(FakeEngine(), fixture(), chunk=1)
    assert first["trace_sha256"] == second["trace_sha256"]


def test_instance_state_isolation():
    first, second = FakeEngine(), FakeEngine()
    MODULE.play(first, fixture())
    assert second.tick == 0 and not second.executed


@pytest.mark.parametrize("failure", ["stalled", "unfinalized", "missing_receipt", "overflow"])
def test_native_failure_is_not_a_success(failure):
    with pytest.raises(ValueError):
        MODULE.play(FakeEngine(**{failure: True}), fixture())


def test_digest_mismatch_rejected(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"first")
    expected = MODULE.digest(path)
    path.write_bytes(b"second")
    with pytest.raises(ValueError):
        MODULE.require_sha(path, expected)


def test_frozen_output_rejected(tmp_path):
    with pytest.raises(ValueError):
        MODULE.NativeProcess(tmp_path, tmp_path / "resident", tmp_path / "new")


def test_aggregate_uses_end_to_end_wall_time():
    match = {"seconds": 1, "reset_seconds": .1, "ticks": 200,
             "cpu_seconds": .8, "peak_rss_kib": 1000}
    result = MODULE.aggregate([{"seconds": 1.2, "matches": [match, match]},
                               {"seconds": 1.3, "matches": [match, match]}], 2, .9)
    assert result["matches_per_second"] == pytest.approx(1.6)
    assert result["scaling_efficiency"] == pytest.approx(1.6 / 1.8)
    assert result["round_seconds_stdev"] > 0
    assert result["completed_matches"] == 4
