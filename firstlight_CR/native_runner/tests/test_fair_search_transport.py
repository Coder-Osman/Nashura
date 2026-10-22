from types import SimpleNamespace

import pytest

from native_runner.cr_native_env import NativeClashEnv, RunnerError
from native_runner.resident_batch_channel import BatchStepFailureV1
from native_runner.training_capture import TrainingAtomicCaptureV1
from native_runner.training.fair_search.transport import CompactSearchNative, create_search_native


def make_native(monkeypatch):
    calls = []
    capture = TrainingAtomicCaptureV1(dict(tick=8), SimpleNamespace())

    class Channel:
        def __init__(self, host, port, **options):
            calls.append(("connect", options))

        def step_partitioned(self, actions, *, ticks):
            calls.append(("step", actions, ticks))
            return {0: SimpleNamespace(capture=capture)}

        def close(self):
            calls.append("close")

        def profile(self):
            return dict(batches=1, response_bytes=100, decoded_response_bytes=200, decode_ns=3, receive_ns=4)

    monkeypatch.setattr("native_runner.training.fair_search.transport.ResidentBatchChannelV1", Channel)

    def request(native, command, **options):
        calls.append(("control", command))
        if command == "multi-status":
            return dict(occupied=1, slots=[dict(envId=0)])
        return dict(tick=7, sha256="a" * 64)

    monkeypatch.setattr(NativeClashEnv, "_request", request)
    native = CompactSearchNative(port=30049)
    native.start_atomic_steps()
    return native, calls, capture


def test_initial_warmup_preserves_control_step_then_enforces_single_tick(monkeypatch):
    native, calls, _ = make_native(monkeypatch)
    native._atomic_steps = False
    native.step(390)
    assert calls == [("control", "env 0 step 390")]
    native.start_atomic_steps()
    with pytest.raises(ValueError, match="one native tick"):
        native.step(390)
    native.step()
    native.close_transport()


def test_compact_step_is_fixed_tick_and_reuses_atomic_capture(monkeypatch):
    native, calls, capture = make_native(monkeypatch)
    assert native.step() is capture.ordinary
    assert native.observe_atomic() is capture
    assert native.observe() is capture.ordinary
    native.step()
    assert sum(isinstance(item, tuple) and item[0] == "connect" for item in calls) == 1
    options = calls[0][1]
    assert options["training_capture"] and options["compressed_json"] and options["validate_json_capture"]
    assert not options["step_until_event"]
    assert ("step", {0: ()}, 1) in calls
    with pytest.raises(ValueError, match="one native tick"):
        native.step(2)
    native.close_transport()


def test_control_closes_batch_and_invalidates_capture_before_action(monkeypatch):
    native, calls, capture = make_native(monkeypatch)
    native.step()
    native._request('inject {"owner":0}')
    assert calls[-2:] == ["close", ("control", 'env 0 inject {"owner":0}')]
    assert native._capture is None
    assert native.observe() != capture.ordinary
    native.step()
    assert sum(isinstance(item, tuple) and item[0] == "connect" for item in calls) == 2
    native.close_transport()


def test_snapshot_control_attests_one_slot_and_uses_bound_manager(monkeypatch):
    native, calls, _ = make_native(monkeypatch)
    native.step()
    assert native._request("state-digest")["sha256"] == "a" * 64
    assert calls[-3:] == ["close", ("control", "multi-status"), ("control", "state-digest")]
    native.resident_status = lambda: dict(occupied=2, slots=[dict(envId=0), dict(envId=1)])
    with pytest.raises(RunnerError, match="exactly one"):
        native.create_snapshot()


def test_compact_failure_closes_channel_and_never_keeps_old_capture(monkeypatch):
    native, calls, _ = make_native(monkeypatch)
    native.step()
    native._batch_channel.step_partitioned = lambda *arguments, **keywords: {
        0: BatchStepFailureV1(0, RunnerError("missing event sequence"))}
    with pytest.raises(RunnerError, match="missing event sequence"):
        native.step()
    assert native._batch_channel is None and native._capture is None
    assert calls[-1] == "close"


def test_json_transport_remains_explicit_baseline():
    native = create_search_native(30049, transport="json")
    assert type(native) is NativeClashEnv and native._persistent_transport_requested
    native.close_transport()
    with pytest.raises(ValueError, match="unknown"):
        create_search_native(30049, transport="other")
