from dataclasses import FrozenInstanceError
import struct

import pytest

import native_runner.training_capture as capture_module
import native_runner.resident_batch_channel as batch_module
from native_runner.training_capture import TrainingCaptureDecoder, TrainingCaptureError, decode_training_capture
from native_runner.resident_batch_channel import ResidentBatchChannelV1, BatchStepFailureV1
from native_runner.battle_env import BattleEnvV1, BattleEnvError
from native_runner.tests.test_training_capture import _capture, _event, _fact
from native_runner.tests.test_resident_batch_channel import _ScriptedSocket, _response_frame


def damage(sequence=1, *, amount=100):
    return _event(sequence=sequence, kind=1, hook_offset=0xF642C4,
                  target=_fact(native_id=23, owner=1, card_id=26000042),
                  requested_amount=amount, actual_amount=amount, pre_hp=1000, post_hp=1000-amount)


def window(events, *, generation=7, epoch=11, first=1, oldest=1, tick=42):
    payload = bytearray(_capture(tuple(events)))
    struct.pack_into('<Q', payload, 8, generation)
    struct.pack_into('<Q', payload, 16, epoch)
    struct.pack_into('<i', payload, 32, tick)
    offset = capture_module._HEADER.size + 2*capture_module._PLAYER.size
    next_sequence = oldest+len(events)
    struct.pack_into('<IQQQQQH', payload, offset, 7, first, oldest, next_sequence,
                     max(0, next_sequence-first-1024), 0, len(events))
    return bytes(payload)


def test_reuses_only_immutable_events_and_preserves_entire_capture():
    decoder = TrainingCaptureDecoder()
    payload = window([damage()])
    first = decoder.decode(payload)
    second = decoder.decode(window([damage()], tick=43))
    assert first == decode_training_capture(payload)
    assert second == decode_training_capture(window([damage()], tick=43))
    assert second.snapshot.combat_events.events[0] is first.snapshot.combat_events.events[0]
    assert second.ordinary is not first.ordinary
    assert decoder.hits == 1 and decoder.misses == 1
    with pytest.raises(FrozenInstanceError):
        second.snapshot.combat_events.events[0].actual_amount = 4


def test_same_sequence_changed_bytes_are_redecoded_and_revalidated():
    decoder = TrainingCaptureDecoder()
    before = decoder.decode(window([damage()]))
    changed = window([damage(amount=101)])
    after = decoder.decode(changed)
    assert after == decode_training_capture(changed)
    assert after.snapshot.combat_events.events[0].actual_amount == 101
    assert after.snapshot.combat_events.events[0] is not before.snapshot.combat_events.events[0]
    with pytest.raises(TrainingCaptureError, match='amount'):
        decoder.decode(window([damage(amount=-1)]))
    assert decoder._events == {}


@pytest.mark.parametrize('kwargs', [{'generation': 8}, {'epoch': 12}, {'first': 2, 'oldest': 2}])
def test_reset_epoch_and_rebind_never_reuse_previous_identity(kwargs):
    decoder = TrainingCaptureDecoder()
    first = decoder.decode(window([damage(1), damage(2)]))
    events = [damage(2)] if 'oldest' in kwargs else [damage(1), damage(2)]
    payload = window(events, **kwargs)
    after = decoder.decode(payload)
    assert after == decode_training_capture(payload)
    assert decoder.hits == 0
    assert after.snapshot.combat_events.events[-1] is not first.snapshot.combat_events.events[-1]


def test_cached_event_tick_is_checked_against_new_observation():
    decoder = TrainingCaptureDecoder()
    decoder.decode(window([damage()]))
    with pytest.raises(TrainingCaptureError, match='event'):
        decoder.decode(window([damage()], tick=41))
    assert decoder._identity is None


def test_ring_rollover_is_bounded_and_preserves_all_retained_events():
    decoder = TrainingCaptureDecoder()
    events = [damage(i) for i in range(1, 1025)]
    decoder.decode(window(events))
    rolled = window(events[1:]+[damage(1025)], oldest=2)
    assert decoder.decode(rolled) == decode_training_capture(rolled)
    assert decoder.hits == 1023
    assert set(decoder._events) == set(range(2, 1026))
    # A restore to an earlier window must also discard now-future entries.
    restored = window([damage()])
    assert decoder.decode(restored) == decode_training_capture(restored)
    assert set(decoder._events) == {1}
    decoder.decode(window([]))
    assert decoder._events == {}


@pytest.mark.parametrize('corrupt', [lambda p: p[:-1], lambda p: p+b'x'])
def test_invalid_frame_after_cache_hits_clears_all_cached_records(corrupt):
    decoder = TrainingCaptureDecoder()
    payload = window([damage()])
    decoder.decode(payload)
    with pytest.raises(TrainingCaptureError):
        decoder.decode(corrupt(payload))
    assert decoder._events == {}
    assert decoder.decode(payload) == decode_training_capture(payload)


def deployed(sequence, *, kind=13, cost=4):
    deployment = struct.pack('<B3xQiIIIiii', 1, 9, 0, 26000024, 26000024,
                             (cost << 28) | (7 << 22), 6, cost, 0)
    return _event(sequence=sequence, kind=kind, hook_offset=0xF38A68,
                  deployment=deployment, target=_fact(native_id=17, owner=0, card_id=26000024))


@pytest.mark.parametrize('new_event,match', [
    (deployed(2), 'card play'),
    (deployed(2, kind=4, cost=5), 'changed identity'),
])
def test_cache_hits_keep_cross_event_deployment_validation(new_event, match):
    decoder = TrainingCaptureDecoder()
    decoder.decode(window([deployed(1)]))
    payload = window([deployed(1), new_event])
    with pytest.raises(TrainingCaptureError, match=match):
        decoder.decode(payload)
    with pytest.raises(TrainingCaptureError, match=match):
        decode_training_capture(payload)
    assert decoder._events == {}


def test_channels_isolate_slots_clear_failed_slot_and_release_on_close(monkeypatch):
    payload = window([damage()])
    handshake = batch_module._HANDSHAKE.pack(batch_module._HANDSHAKE_MAGIC, 1, 0, 16, 0)
    same = ((0, 0, payload), (1, 0, payload))
    stream = handshake + b''.join([
        _response_frame(1, same), _response_frame(2, same),
        _response_frame(3, ((0, 0, payload+b'x'), (1, 0, payload))),
        _response_frame(4, same),
    ])
    socket = _ScriptedSocket(stream)
    monkeypatch.setattr(batch_module.socket, 'create_connection', lambda *a, **k: socket)
    channel = ResidentBatchChannelV1(training_capture=True)
    initial = channel.step({0: (), 1: ()})
    original = [initial[i].capture.snapshot.combat_events.events[0] for i in (0, 1)]
    assert original[0] is not original[1]
    warm = channel.step({0: (), 1: ()})
    assert all(warm[i].capture.snapshot.combat_events.events[0] is original[i] for i in (0, 1))
    failed = channel.step_partitioned({0: (), 1: ()})
    assert isinstance(failed[0], BatchStepFailureV1)
    assert 0 not in channel._training_decoders and 1 in channel._training_decoders
    recovered = channel.step({0: (), 1: ()})
    assert recovered[0].capture.snapshot.combat_events.events[0] is not original[0]
    assert recovered[1].capture.snapshot.combat_events.events[0] is original[1]
    channel.close()
    assert channel._training_decoders == {} and socket.closed


def phase_window(sequences, *, tick=42, epoch=11, oldest=1, first=1, event_tick=42, output=100):
    payload = bytearray(_capture())
    struct.pack_into('<Q', payload, 16, epoch)
    struct.pack_into('<i', payload, 32, tick)
    offset = capture_module._HEADER.size + 2*capture_module._PLAYER.size + capture_module._COMBAT_ENVELOPE.size
    next_sequence = oldest + len(sequences)
    header = capture_module._PHASE_ENVELOPE.pack(7, first, oldest, next_sequence,
                                                max(0, next_sequence-first-4096), 0, len(sequences))
    events = [capture_module._PHASE_EVENT.pack(sequence, event_tick, 5, 1, 0, 0x1234, 0,
                                               0, 17, -1, 0, *([capture_module._NONE_I32]*5),
                                               output, *([capture_module._NONE_I32]*4))
              for sequence in sequences]
    return bytes(payload[:offset]) + header + b''.join(events) + bytes(payload[offset+capture_module._PHASE_ENVELOPE.size:])


def test_phase_cache_preserves_complete_capture_and_checks_changed_bytes():
    decoder = TrainingCaptureDecoder()
    first = decoder.decode(phase_window([1, 2]))
    warm = decoder.decode(phase_window([1, 2, 3], tick=43))
    assert warm == decode_training_capture(phase_window([1, 2, 3], tick=43))
    assert warm.snapshot.phase_runtime.events[0] is first.snapshot.phase_runtime.events[0]
    assert decoder.phase_hits == 2 and decoder.phase_misses == 3
    changed = decoder.decode(phase_window([1, 2, 3], tick=43, output=101))
    assert changed == decode_training_capture(phase_window([1, 2, 3], tick=43, output=101))
    assert changed.snapshot.phase_runtime.events[0] is not warm.snapshot.phase_runtime.events[0]
    with pytest.raises(TrainingCaptureError, match="phase event"):
        decoder.decode(phase_window([1, 2, 3], tick=41, output=101))
    assert decoder._phase_events == {} and decoder._events == {}


def test_phase_cache_is_bounded_and_invalidated_on_restore_epoch():
    decoder = TrainingCaptureDecoder()
    decoder.decode(phase_window(range(1, 4097)))
    rolled = phase_window(range(2, 4098), oldest=2)
    assert decoder.decode(rolled) == decode_training_capture(rolled)
    assert len(decoder._phase_events) == 4096
    assert decoder.phase_hits == 4095
    restored = decoder.decode(phase_window([1], epoch=12))
    assert restored == decode_training_capture(phase_window([1], epoch=12))
    assert set(decoder._phase_events) == {1}
    decoder.clear()
    assert decoder._phase_identity is None and decoder._phase_events == {}


def delta_window(events, *, transmit=1, next_sequence=None, epoch=11, tick=42, first=1, oldest=1):
    payload = bytearray(_capture(tuple(events)))
    struct.pack_into("<H", payload, 4, 5)
    struct.pack_into("<Q", payload, 16, epoch)
    struct.pack_into("<i", payload, 32, tick)
    offset = capture_module._HEADER.size + 2 * capture_module._PLAYER.size
    next_sequence = transmit + len(events) if next_sequence is None else next_sequence
    header = capture_module._COMBAT_DELTA_ENVELOPE.pack(
        7, first, oldest, next_sequence, max(0, next_sequence-first-1024), 0, transmit, len(events))
    return bytes(payload[:offset]) + header + bytes(payload[offset+capture_module._COMBAT_ENVELOPE.size:])


def test_delta_retains_validation_identity_and_returns_only_new_events():
    decoder = TrainingCaptureDecoder()
    first = decoder.decode(delta_window([damage()]))
    assert decoder.cursor() == (7, 11, 2)
    after = decoder.decode(delta_window([damage(2)], transmit=2, tick=43))
    reference = decode_training_capture(window([damage(), damage(2)], tick=43))
    assert after.ordinary == reference.ordinary
    assert after.snapshot.combat_events.events == reference.snapshot.combat_events.events[1:]
    assert after.snapshot.combat_events.transmit_from_sequence == 2
    assert first.snapshot.combat_events.events[0] == reference.snapshot.combat_events.events[0]
    assert decoder._events == {}
    empty = decoder.decode(delta_window([], transmit=3, tick=44))
    assert empty.snapshot.combat_events.events == ()
    assert decoder.cursor() == (7, 11, 3)


@pytest.mark.parametrize("transmit,next_sequence", [(3, 4), (1, 2), (2, 1)])
def test_delta_rejects_skipped_duplicate_and_regressed_sequences(transmit, next_sequence):
    decoder = TrainingCaptureDecoder()
    decoder.decode(delta_window([damage()]))
    with pytest.raises(TrainingCaptureError):
        decoder.decode(delta_window([damage(transmit)], transmit=transmit, next_sequence=next_sequence))
    assert decoder.cursor() == (0, 0, 0)


def test_delta_restore_requires_a_new_complete_prefix_and_checks_truncation():
    decoder = TrainingCaptureDecoder()
    decoder.decode(delta_window([damage()]))
    decoder.decode(delta_window([damage(10)], epoch=12, first=10, oldest=10, transmit=10))
    assert decoder.cursor() == (7, 12, 11)
    with pytest.raises(TrainingCaptureError, match="prefix"):
        decoder.decode(delta_window([damage(12)], epoch=13, transmit=12))
    with pytest.raises(TrainingCaptureError, match="persistent"):
        decode_training_capture(delta_window([damage()]))
    decoder.decode(delta_window([damage()]))
    with pytest.raises(TrainingCaptureError, match="trailing"):
        decoder.decode(delta_window([], transmit=2) + b"x")
    assert decoder.cursor() == (0, 0, 0)


@pytest.mark.parametrize("new_event,match", [
    (deployed(2), "card play"),
    (deployed(2, kind=4, cost=5), "changed identity"),
])
def test_delta_preserves_cross_frame_deployment_checks(new_event, match):
    decoder = TrainingCaptureDecoder()
    decoder.decode(delta_window([deployed(1)]))
    with pytest.raises(TrainingCaptureError, match=match):
        decoder.decode(delta_window([new_event], transmit=2))


def test_delta_cursor_survives_channel_reopen_and_slots_stay_isolated(monkeypatch):
    handshake = batch_module._HANDSHAKE.pack(batch_module._HANDSHAKE_MAGIC, 1, 0, 16, 0)
    sockets = [
        _ScriptedSocket(handshake + _response_frame(1, ((0, 0, delta_window([damage()])),))),
        _ScriptedSocket(handshake + _response_frame(1, ((0, 0, delta_window([damage(2)], transmit=2)),))),
    ]
    monkeypatch.setattr(batch_module.socket, "create_connection", lambda *args, **kwargs: sockets.pop(0))
    decoders = {}
    first = ResidentBatchChannelV1(training_capture=True, event_deltas=True, training_decoders=decoders)
    first.step({0: ()}, ticks=1)
    first.close()
    assert decoders[0].cursor() == (7, 11, 2)
    second = ResidentBatchChannelV1(training_capture=True, event_deltas=True, training_decoders=decoders)
    second.step({0: ()}, ticks=1)
    request = bytes(second._socket.sent)[len(b"batch-fast-v1\n"):]
    assert batch_module._HEADER.unpack_from(request)[2] == 2
    assert batch_module._REQUEST_CURSOR.unpack_from(request, batch_module._HEADER.size +
                                                   batch_module._REQUEST_ENTRY.size) == (7, 11, 2)
    assert decoders[0].cursor() == (7, 11, 3)
    second.close()


def test_delta_consumer_rejects_missing_prefix_even_when_decoder_accepts():
    decoder = TrainingCaptureDecoder()
    decoder.decode(delta_window([damage()]))
    envelope = decoder.decode(delta_window([damage(2)], transmit=2)).snapshot.combat_events
    environment = object.__new__(BattleEnvV1)
    environment.native = object()
    with pytest.raises(BattleEnvError, match="consumer cursor"):
        environment._checked_ring_sequence(envelope, 1, label="combat")
    assert environment._checked_ring_sequence(envelope, 2, label="combat") == 2


def test_delta_never_silently_falls_back_to_full_capture(monkeypatch):
    handshake = batch_module._HANDSHAKE.pack(batch_module._HANDSHAKE_MAGIC, 1, 0, 16, 0)
    scripted = _ScriptedSocket(handshake + _response_frame(1, ((0, 0, window([damage()])),)))
    monkeypatch.setattr(batch_module.socket, "create_connection", lambda *args, **kwargs: scripted)
    with ResidentBatchChannelV1(training_capture=True, event_deltas=True) as channel:
        outcome = channel.step_partitioned({0: ()}, ticks=1)[0]
        assert isinstance(outcome, BatchStepFailureV1)
        assert "V5 compact" in str(outcome.error)
        assert not channel._training_decoders
