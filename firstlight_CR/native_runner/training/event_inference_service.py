"""Host-local bounded microbatch inference, with request-local sampling streams.

Only the host supervisor loads weights, at the completed-round barrier. Workers
use a private Unix socket (directory 0700) and send CPU tensor records. Pickle is
restricted to this same-user local transport, never a network listener.
"""
from __future__ import annotations

from concurrent.futures import Future
from collections import deque
from dataclasses import fields, is_dataclass, replace
import json
import io
import os
from pathlib import Path
import pickle
import queue
import socket
import struct
import threading
import time
from types import SimpleNamespace

import torch

from .event_inference import InferenceModel
from .event_ppo import _stack_frames

MAX_MESSAGE = 32 * 1024 * 1024
IDENTITY_FIELDS = ("artifact_sha256", "source_checkpoint_sha256", "contract_sha256")
SAMPLING_PROTOCOL = "per-match-cpu-uniform4.v2"


def identity(descriptor):
    return tuple(descriptor[key] for key in IDENTITY_FIELDS)


def _read_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise EOFError("inference service disconnected")
        result.extend(part)
    return result


class TensorPickler(pickle.Pickler):
    def reducer_override(self, obj):
        if isinstance(obj, torch.Tensor):
            if obj.device.type != "cpu":
                raise ValueError("inference wire accepts CPU tensors only")
            # Tensor's normal pickle serializes each storage as a separate torch
            # archive. NumPy's protocol-5 buffer avoids that per-tensor overhead.
            # Use a stable imported callable, including when the service itself
            # runs as __main__ via python -m in a different worker process.
            return torch.from_numpy, (obj.detach().numpy(),)
        return NotImplemented


def send(sock, value):
    buffer = io.BytesIO()
    TensorPickler(buffer, protocol=5).dump(value)
    payload = buffer.getvalue()
    if len(payload) > MAX_MESSAGE:
        raise ValueError("inference message too large")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def receive(sock, *, wait_for_request=False):
    prefix = b""
    if wait_for_request:
        # An idle persistent client can legitimately wait through collection,
        # learning and archiving. Once a frame starts, retain the socket deadline
        # so a stalled partial request still closes the connection.
        timeout = sock.gettimeout()
        sock.settimeout(None)
        try:
            prefix = _read_exact(sock, 1)
        finally:
            sock.settimeout(timeout)
    size, = struct.unpack("!I", prefix + _read_exact(sock, 4 - len(prefix)))
    if size > MAX_MESSAGE:
        raise ValueError("inference message too large")
    return pickle.loads(_read_exact(sock, size))


def tensor_map(value, fn):
    if isinstance(value, torch.Tensor):
        return fn(value)
    if is_dataclass(value):
        return type(value)(**{f.name: tensor_map(getattr(value, f.name), fn) for f in fields(value)})
    return value


@torch.inference_mode()
def infer_batch(model, requests):
    device = next(model.parameters()).device
    temperatures = tuple(requests[0]["temperatures"])
    if any(tuple(r["temperatures"]) != temperatures for r in requests):
        raise ValueError("inference batch crosses temperatures")
    packed = _stack_frames(tuple(r["frame"] for r in requests)).to_model_input(device, float_dtype=torch.float32)
    masks = {name: torch.cat([r[name] for r in requests]).to(device)
             for name in ("gate_mask", "card_mask", "cell_masks", "delay_mask")}
    context = model.encode(packed, model.initial_state(len(requests), device=device),
                           episode_start=torch.ones(len(requests), dtype=torch.bool, device=device))
    step = model.select_action(context, **masks, temperatures=temperatures,
                               sample_uniforms=torch.cat([r["uniforms"] for r in requests]).to(device))
    cpu_step = tensor_map(step, lambda tensor: tensor.detach().cpu())
    return [tensor_map(cpu_step, lambda tensor: tensor[i:i+1].clone()) for i in range(len(requests))]


class Client:
    def __init__(self, path, timeout=120.):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.socket.connect(str(path))
        self.sequence = 0
        self.profile = {}

    def request(self, operation, **payload):
        self.sequence += 1
        send(self.socket, dict(operation=operation, request_id=self.sequence, **payload))
        reply = receive(self.socket)
        if reply.get("request_id") != self.sequence:
            raise ValueError("inference response request identity mismatch")
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "inference request failed"))
        return reply

    def start_match(self, descriptor, seed):
        reply = self.request("describe", identity=identity(descriptor))
        self.identity = identity(descriptor)
        self.generator = torch.Generator(device="cpu").manual_seed(seed)
        self.profile = dict(requests=0, roundtrip_seconds=0., queue_seconds=0., batch_seconds=0., batch_rows=0)
        return reply["model"], reply["policy_version"]

    def infer(self, frame, *, owner, tick, **masks):
        started = time.perf_counter()
        reply = self.request("infer", identity=self.identity, frame=frame, owner=owner, tick=tick,
                             uniforms=torch.rand((1, 4), generator=self.generator, dtype=torch.float64), **masks)
        if tuple(reply.get("identity", ())) != self.identity:
            raise ValueError("inference response behavior identity mismatch")
        self.profile["requests"] += 1
        self.profile["roundtrip_seconds"] += time.perf_counter() - started
        for name in ("queue_seconds", "batch_seconds", "batch_rows"):
            self.profile[name] += reply[name]
        return reply["step"]

    def close(self):
        self.socket.close()


class Service:
    def __init__(self, device="cuda:0", max_batch=16, wait_ms=2.):
        if not 1 <= max_batch <= 64 or not 0 <= wait_ms <= 20:
            raise ValueError("invalid inference batch limits")
        self.device = torch.device(device)
        self.max_batch = max_batch
        self.wait_seconds = wait_ms / 1000.
        self.weights = InferenceModel()
        self.identity = None
        self.policy_version = None
        self.models = {}
        self.round_id = None
        self.inbox = queue.Queue(maxsize=256)
        self.stop = False

    def connection(self, sock):
        sock.settimeout(180.)
        last_id = 0
        try:
            while True:
                request = receive(sock, wait_for_request=True)
                if type(request.get("request_id")) is not int or request["request_id"] <= last_id:
                    raise ValueError("non-monotonic request id")
                last_id = request["request_id"]
                future = Future()
                self.inbox.put((request, future, time.perf_counter()), timeout=120.)
                send(sock, future.result(timeout=150.))
        except (EOFError, OSError, TimeoutError, ValueError, queue.Full):
            pass
        finally:
            sock.close()

    def control(self, request):
        operation = request["operation"]
        if operation in {"load", "load_set"} and self.round_id is not None:
            raise ValueError("model set is frozen during an active round")
        if operation == "load_set":
            descriptors = request["descriptors"]
            if not descriptors:
                raise ValueError("model set must not be empty")
            # Build off to the side; a failed load publishes no partial set.
            models = {}
            for descriptor in descriptors:
                key = identity(descriptor)
                if key in models:
                    raise ValueError("duplicate model identity")
                if key in self.models:
                    models[key] = self.models[key]
                else:
                    weights = InferenceModel()
                    model, version = weights.load(descriptor)
                    model.to(self.device).eval()
                    models[key] = (weights, version)
            # Historical card-only opponents can face skill-enabled actors.
            # Each identity is batched separately; all other model/input and
            # timing contracts must still match before publishing the set.
            signatures = {(w.model.backbone.config, w.model.event_config,
                           replace(w.model.config, ability_actions=False, ability_delay=False)) for w, _ in models.values()}
            if len(signatures) != 1:
                raise ValueError("model set has incompatible input or action timing contracts")
            self.models = models
            self.identity = None
            return dict(identities=list(models))
        if operation == "begin_round":
            if self.round_id is not None or not self.models or not request.get("round_id"):
                raise ValueError("invalid round start")
            self.round_id = request["round_id"]
            return dict(round_id=self.round_id)
        if operation == "end_round":
            if self.round_id is None or request.get("round_id") != self.round_id:
                raise ValueError("round barrier identity mismatch")
            self.round_id = None
            return {}
        if operation == "load":
            descriptor = request["descriptor"]
            # A rejected load may have copied some tensors before raising.
            # No subsequent request may use the previous identity in that case.
            self.identity = None
            self.models = {}
            model, self.policy_version = self.weights.load(descriptor)
            model.to(self.device).eval()
            self.identity = identity(descriptor)
            self.models[self.identity] = (self.weights, self.policy_version)
            return dict(identity=self.identity, policy_version=self.policy_version)
        key = tuple(request.get("identity", ()))
        if key not in self.models:
            raise ValueError("stale inference behavior identity")
        if operation != "describe":
            raise ValueError("unknown inference operation")
        weights, version = self.models[key]
        model = weights.model
        metadata = SimpleNamespace(config=model.config, event_config=model.event_config, card_count=model.card_count,
                                   cell_count=model.cell_count, backbone=SimpleNamespace(catalog=model.backbone.catalog))
        return dict(model=metadata, policy_version=version)

    def run(self, path):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(128)
        listener.settimeout(.5)

        def accept():
            while not self.stop:
                try:
                    sock, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                threading.Thread(target=self.connection, args=(sock,), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()
        print(json.dumps(dict(ready=True, socket=str(path))), flush=True)
        pending = deque()
        try:
            while True:
                item = pending.popleft() if pending else self.inbox.get()
                request, future, queued = item
                if request["operation"] == "shutdown":
                    future.set_result(dict(ok=True, request_id=request["request_id"]))
                    return
                batch = [item]
                try:
                    if request["operation"] != "infer":
                        future.set_result(dict(ok=True, request_id=request["request_id"], **self.control(request)))
                        continue
                    key = tuple(request["identity"])
                    if key not in self.models:
                        raise ValueError("stale inference behavior identity")
                    deadline = time.perf_counter() + self.wait_seconds
                    deferred = []
                    while len(batch) < self.max_batch:
                        try:
                            other = pending.popleft() if pending else self.inbox.get(timeout=max(0., deadline - time.perf_counter()))
                        except queue.Empty:
                            break
                        candidate = other[0]
                        if candidate["operation"] != "infer":
                            pending.appendleft(other)
                            break
                        if (candidate["identity"] != request["identity"]
                                or candidate["temperatures"] != request["temperatures"]):
                            deferred.append(other)
                            if time.perf_counter() >= deadline:
                                break
                            continue
                        batch.append(other)
                    pending.extendleft(reversed(deferred))
                    started = time.perf_counter()
                    steps = infer_batch(self.models[key][0].model, [entry[0] for entry in batch])
                    elapsed = time.perf_counter() - started
                    for (req, result, arrival), step in zip(batch, steps):
                        result.set_result(dict(ok=True, request_id=req["request_id"], identity=key,
                                               step=step, queue_seconds=started-arrival,
                                               batch_seconds=elapsed, batch_rows=len(batch)))
                except Exception as error:
                    for req, result, _ in batch:
                        result.set_result(dict(ok=False, request_id=req["request_id"], error=repr(error)))
        finally:
            self.stop = True
            listener.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--max-batch", type=int, default=16)
    parser.add_argument("--wait-ms", type=float, default=2.)
    args = parser.parse_args()
    torch.set_num_threads(1)
    Service(args.device, args.max_batch, args.wait_ms).run(Path(args.socket))


if __name__ == "__main__":
    main()
