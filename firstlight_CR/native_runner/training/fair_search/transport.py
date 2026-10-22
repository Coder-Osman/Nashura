from collections.abc import Mapping

from native_runner.cr_native_env import NativeClashEnv, ResidentNativeClashEnv, RunnerError
from native_runner.resident_batch_channel import BatchStepFailureV1, ResidentBatchChannelV1
from native_runner.training_capture import TrainingAtomicCaptureV1


class CompactSearchNative(ResidentNativeClashEnv):
    def __init__(self, *, port, timeout=90., event_deltas=False):
        super().__init__(0, port=port, timeout=timeout)
        self._batch_channel = None
        self._capture = None
        self._atomic_steps = False
        self._persistent_transport_requested = True
        self._event_deltas = event_deltas
        self._training_decoders = {}
        self.capture_profile = dict(batches=0, response_bytes=0, decoded_response_bytes=0,
                                    decode_ns=0, receive_ns=0)

    def start_atomic_steps(self):
        with self._lock:
            self._capture = None
            self._atomic_steps = True

    def _close_batch(self):
        channel, self._batch_channel = self._batch_channel, None
        if channel is not None:
            profile = channel.profile()
            for name in self.capture_profile:
                self.capture_profile[name] += profile[name]
            channel.close()

    def _request(self, command, *, _read_retry_count=2):
        with self._lock:
            self._close_batch()
            self._capture = None
            if command in {"state-digest", "snapshot-create"} or command.startswith(("restore ", "release-snapshot ")):
                status = self.resident_status()
                slots = status.get("slots", ())
                if status.get("occupied") != 1 or len(slots) != 1 or slots[0].get("envId") != 0:
                    raise RunnerError("FAIR compact control requires exactly one resident slot")
                return self._raw_request(command, _read_retry_count=_read_retry_count)
            return super()._request(command, _read_retry_count=_read_retry_count)

    def step(self, ticks=1):
        if not self._atomic_steps:
            return super().step(ticks)
        if type(ticks) is not int or ticks != 1:
            raise ValueError("FAIR compact transport requires one native tick")
        with self._lock:
            self._capture = None
            if self._batch_channel is None:
                super().close_transport()
                self._batch_channel = ResidentBatchChannelV1(
                    self.host, self.port, timeout=self.timeout, training_capture=True,
                    compressed_json=True, validate_json_capture=True, step_until_event=False,
                    event_deltas=self._event_deltas, training_decoders=self._training_decoders)
            try:
                outcome = self._batch_channel.step_partitioned({0: ()}, ticks=1)[0]
                if isinstance(outcome, BatchStepFailureV1):
                    raise outcome.error
                capture = outcome.capture
                ordinary = capture.ordinary if isinstance(capture, TrainingAtomicCaptureV1) else capture["ordinary"]
                if not isinstance(ordinary, Mapping):
                    raise RunnerError("FAIR compact response has no ordinary state")
                self._capture = capture
                return ordinary
            except BaseException:
                self._close_batch()
                self._training_decoders.clear()
                raise

    def observe(self):
        if self._capture is not None:
            return (self._capture.ordinary if isinstance(self._capture, TrainingAtomicCaptureV1)
                    else self._capture["ordinary"])
        return super().observe()

    def observe_atomic(self):
        if self._capture is not None:
            return self._capture
        return super().observe_atomic()

    def close_transport(self):
        with self._lock:
            self._close_batch()
            self._capture = None
            self._training_decoders.clear()
            super().close_transport()


def create_search_native(port, *, transport="compact"):
    if transport in ("compact", "compact-delta"):
        return CompactSearchNative(port=port, event_deltas=transport == "compact-delta")
    if transport != "json":
        raise ValueError("unknown FAIR native transport")
    native = NativeClashEnv(port=port, timeout=90.)
    native._persistent_transport_requested = True
    return native
