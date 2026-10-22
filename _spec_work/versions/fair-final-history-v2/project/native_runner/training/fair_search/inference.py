from concurrent.futures import Future
from collections import OrderedDict
from dataclasses import fields, is_dataclass
import math
import os
from pathlib import Path
import queue
import random
import socket
import struct
import threading
import time

import torch

from ..event_inference_service import Client, receive, send
from ..event_ppo import _stack_frames
from ..v4.tensors import concatenate_tensor_records
from ..v4.model import _distribution
from .config import SearchConfig
from .contracts import InformationState, require_sha
from .learner import decode_action


def action_key(action):
    if not bool(action.act[0]):
        return "wait"
    return f"play:{int(action.card_row[0])}:{int(action.cell[0])}:{int(action.delay_bin[0])}"


def uniform_action(information, rng):
    masks = information.masks
    if not bool(masks["gate_mask"][0, 1]) or rng.random() < .1:
        return "wait"
    rows = masks["card_mask"][0].nonzero().flatten().tolist()
    rows = [row for row in rows if bool(masks["cell_masks"][0, row].any() and masks["delay_mask"][0, row].any())]
    if not rows:
        return "wait"
    row = rng.choice(rows)
    cell = rng.choice(masks["cell_masks"][0, row].nonzero().flatten().tolist())
    delay = rng.choice(masks["delay_mask"][0, row].nonzero().flatten().tolist())
    return f"play:{row}:{cell}:{delay}"


def tensor_bytes(value):
    if isinstance(value, torch.Tensor):
        return value.numel() * value.element_size()
    if is_dataclass(value):
        return sum(tensor_bytes(getattr(value, field.name)) for field in fields(value))
    if isinstance(value, dict):
        return sum(tensor_bytes(item) for item in value.values())
    return 0


class LocalInference:
    def __init__(self, model, identity, *, config=None, cache_entries=64,
                 cache_bytes=256 * 1024 * 1024, score_batch=32, fast_sampling=True):
        self.model, self.identity = model.eval(), require_sha(identity)
        self.config = config or SearchConfig()
        self.model.requires_grad_(False)
        self.model_identity = self.identity
        self.versions = self._versions()
        if (type(cache_entries) is not int or cache_entries < 0
                or type(cache_bytes) is not int or cache_bytes < 0
                or type(score_batch) is not int or not 1 <= score_batch <= 32):
            raise ValueError("invalid inference cache or scoring bounds")
        self.cache_entries, self.cache_bytes, self.score_batch = cache_entries, cache_bytes, score_batch
        self.fast_sampling = fast_sampling
        self.cache = OrderedDict()
        self.profile = dict(requests=0, cache_hits=0, encoded_rows=0, evictions=0,
                            cache_bytes=0, score_rows=0, score_batches=0, score_hits=0,
                            encode_seconds=0., sample_seconds=0., score_seconds=0., key_seconds=0.)

    def _versions(self):
        return tuple((name, id(tensor), tensor._version, tensor.device, tensor.dtype)
                     for name, tensor in (*self.model.named_parameters(), *self.model.named_buffers()))

    def validate(self):
        if (self.identity != self.model_identity or self._versions() != self.versions
                or any(module.training for module in self.model.modules())
                or any(parameter.requires_grad for parameter in self.model.parameters())):
            raise ValueError("inference model changed or is not frozen")

    def clear_cache(self):
        self.cache.clear()
        self.profile["cache_bytes"] = 0

    def _remember(self, key, entry):
        size = tensor_bytes(entry)
        if not self.cache_entries or size > self.cache_bytes:
            return
        while self.cache and (len(self.cache) >= self.cache_entries
                              or self.profile["cache_bytes"] + size > self.cache_bytes):
            _, oldest = self.cache.popitem(last=False)
            self.profile["cache_bytes"] -= tensor_bytes(oldest)
            self.profile["evictions"] += 1
        self.cache[key] = entry
        self.profile["cache_bytes"] += size

    def _contexts(self, requests):
        entries = [None] * len(requests)
        missing = []
        for index, request in enumerate(requests):
            started = time.perf_counter()
            key = (self.identity, request["information"].sha256) if self.cache_entries else None
            self.profile["key_seconds"] += time.perf_counter() - started
            if key is not None and key in self.cache:
                entries[index] = self.cache[key]
                self.cache.move_to_end(key)
                self.profile["cache_hits"] += 1
            else:
                missing.append((index, key))
        if missing:
            started = time.perf_counter()
            context, logits = self.model.encode(_stack_frames(
                tuple(requests[index]["information"].frame for index, _ in missing)))
            values = self.model.value(logits).float().cpu().tolist()
            device = next(self.model.parameters()).device
            for offset, (index, key) in enumerate(missing):
                info = requests[index]["information"]
                entry = dict(context=context.index_select(torch.tensor([offset], device=device)),
                             value=values[offset], masks={name: mask.to(device, copy=True)
                                                         for name, mask in info.masks.items()}, logs={})
                entries[index] = entry
                if key is not None:
                    if key in self.cache:
                        self.profile["cache_bytes"] -= tensor_bytes(self.cache.pop(key))
                    self._remember(key, entry)
            self.profile["encoded_rows"] += len(missing)
            self.profile["encode_seconds"] += time.perf_counter() - started
        return entries

    def _score(self, actions, entry, device):
        logs = entry["logs"]
        missing = [action for action in actions if action not in logs]
        self.profile["score_hits"] += len(actions) - len(missing)
        for offset in range(0, len(missing), self.score_batch):
            chunk = missing[offset:offset + self.score_batch]
            count = len(chunk)
            context = entry["context"] if count == 1 else entry["context"].index_select(
                torch.zeros(count, dtype=torch.long, device=device))
            masks = {name: mask.expand(count, *mask.shape[1:]) for name, mask in entry["masks"].items()}
            action = concatenate_tensor_records(tuple(decode_action(key, device) for key in chunk))
            step = self.model.policy.evaluate_actions(context, action, **masks, validate=True)
            logs.update(zip(chunk, step.log_prob.float().cpu().tolist()))
            self.profile["score_rows"] += count
            self.profile["score_batches"] += 1
        result = {action: logs[action] for action in actions}
        if len(logs) > 512:
            entry["logs"] = dict(result)
        return result

    def _sample_action(self, entry, uniforms, cache):
        policy, context, masks = self.model.policy, entry["context"], entry["masks"]

        def draw(key, head, distribution):
            if key not in cache:
                cache[key] = distribution().probs.to(torch.float64).cumsum(dim=-1).cpu()
            cdf = cache[key]
            threshold = uniforms[:, head:head + 1].to(cdf) * cdf[:, -1:]
            threshold = torch.minimum(threshold, torch.nextafter(cdf[:, -1:], torch.zeros_like(threshold)))
            return (cdf <= threshold).sum(dim=-1).clamp_max(cdf.shape[-1] - 1)

        def gate_distribution():
            logits, mask = policy.event_gate(context, masks["gate_mask"])
            return _distribution(logits, mask=mask, temperature=1., validate=True)

        gate = draw("gate", 0, gate_distribution)
        if int(gate[0]) == 0:
            return "wait"
        card = draw("card", 1, lambda: _distribution(policy.card_logits(context),
                    mask=masks["card_mask"], temperature=1., validate=True))
        row = int(card[0])
        device = context.value_context.device

        def cell_distribution():
            selected_card = torch.tensor([row], device=device)
            return _distribution(policy.cell_logits(context, selected_card),
                mask=policy._mask_for_card(masks["cell_masks"], selected_card, label="cell_masks"),
                temperature=1., validate=True)

        cell = draw(("cell", row), 2, cell_distribution)
        placement = int(cell[0])
        delay = draw(("delay", row, placement), 3, lambda: policy.action_delay_distribution(
            context, torch.tensor([row], device=device), torch.tensor([placement], device=device),
            None, masks["delay_mask"], 1., True))
        return f"play:{row}:{placement}:{int(delay[0])}"

    @torch.inference_mode()
    def infer_batch(self, requests):
        self.validate()
        if not requests or any(request["operation"] not in {"predict", "sample"} for request in requests):
            raise ValueError("empty or unknown search inference operation")
        entries = self._contexts(requests)
        self.profile["requests"] += len(requests)
        device = next(self.model.parameters()).device
        replies = []
        for index, request in enumerate(requests):
            info = request["information"]
            entry = entries[index]
            single, masks = entry["context"], entry["masks"]
            rng = random.Random(request["seed"])
            sampling_cache = {}

            def sample():
                started = time.perf_counter()
                uniforms = torch.tensor([[rng.random() for _ in range(4)]],
                                        device="cpu" if self.fast_sampling else device)
                if self.fast_sampling:
                    action = self._sample_action(entry, uniforms, sampling_cache)
                else:
                    step = self.model.policy.select_action(single, **masks, sample=True,
                                                           sample_uniforms=uniforms, validate=True)
                    action = action_key(step.action)
                if not info.legal(action):
                    raise ValueError("inference sample violates FAIR masks")
                self.profile["sample_seconds"] += time.perf_counter() - started
                return action

            if request["operation"] == "sample":
                replies.append(dict(action=sample()))
                continue
            if request["operation"] != "predict":
                raise ValueError("unknown search inference operation")
            actions = {"wait"}
            for attempt in range(256):
                if len(actions) >= self.config.candidates:
                    break
                if not bool(info.masks["gate_mask"][0, 1]):
                    break
                actions.add(uniform_action(info, rng) if rng.random() < .2 else sample())
            for action in sorted(actions):
                if not info.legal(action):
                    raise ValueError("inference candidate violates FAIR masks")
            started = time.perf_counter()
            logs = self._score(sorted(actions), entry, device)
            self.profile["score_seconds"] += time.perf_counter() - started
            maximum = max(logs.values())
            prior = {action: math.exp(logp - maximum) for action, logp in logs.items()}
            replies.append(dict(prior=prior, value=entry["value"]))
        return replies

    def predict(self, information, *, seed):
        reply = self.infer_batch([dict(operation="predict", information=information, seed=seed)])[0]
        return reply["prior"], reply["value"]

    def sample(self, information, *, seed):
        return self.infer_batch([dict(operation="sample", information=information, seed=seed)])[0]["action"]


class BatchInference:
    def __init__(self, models, *, config=None):
        self.config = config or SearchConfig()
        self.models = dict(models)
        if not self.models or any(identity != backend.identity for identity, backend in self.models.items()):
            raise ValueError("batch inference model identities differ")
        self.queue = queue.Queue(maxsize=self.config.inference_queue)
        self.stopping = threading.Event()
        self.profile = dict(batches=0, rows=0, max_batch=0, infer_seconds=0.)
        self.thread = threading.Thread(target=self.work, name="fair-search-inference")
        self.thread.start()

    def submit(self, identity, operation, information, seed):
        if self.stopping.is_set() or identity not in self.models or operation not in {"predict", "sample"}:
            raise ValueError("unknown, stopped or mismatched inference service")
        future = Future()
        self.queue.put((identity, dict(operation=operation, information=information, seed=seed), future), timeout=5.)
        return future

    def work(self):
        torch.set_num_threads(1)
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                pending = [self.queue.get(timeout=.05)]
            except queue.Empty:
                continue
            deadline = time.monotonic() + self.config.inference_wait_ms / 1000.
            while len(pending) < self.config.inference_batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    pending.append(self.queue.get(timeout=remaining))
                except queue.Empty:
                    break
            groups = {}
            for identity, request, future in pending:
                groups.setdefault(identity, []).append((request, future))
            for identity, entries in groups.items():
                started = time.perf_counter()
                try:
                    replies = self.models[identity].infer_batch([request for request, _ in entries])
                    if len(replies) != len(entries):
                        raise ValueError("inference response count differs")
                    for (_, future), reply in zip(entries, replies):
                        future.set_result(reply)
                    self.profile["batches"] += 1
                    self.profile["rows"] += len(entries)
                    self.profile["max_batch"] = max(self.profile["max_batch"], len(entries))
                    self.profile["infer_seconds"] += time.perf_counter() - started
                except BaseException as error:
                    for _, future in entries:
                        future.set_exception(error)

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=120.)
        if self.thread.is_alive():
            raise TimeoutError("inference worker did not stop")


class SocketInferenceClient(Client):
    def __init__(self, path, identity, timeout=120.):
        super().__init__(path, timeout=timeout)
        self.identity = require_sha(identity)

    def query(self, operation, information, seed):
        reply = self.request(operation, identity=self.identity, information=information.to_payload(), seed=seed)
        if reply["identity"] != self.identity or reply["information_sha256"] != information.sha256:
            raise ValueError("inference reply information or model differs")
        return reply

    def predict(self, information, *, seed):
        reply = self.query("predict", information, seed)
        return reply["prior"], reply["value"]

    def sample(self, information, *, seed):
        return self.query("sample", information, seed)["action"]


class SocketInferenceServer:
    def __init__(self, directory, batch):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.path = directory / "inference.sock"
        self.batch = batch
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.bind(str(self.path))
            self.socket.listen(64)
        except BaseException:
            self.socket.close()
            self.batch.close()
            raise
        self.socket.settimeout(.1)
        self.connections, self.handlers = [], []
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.accept, name="fair-search-accept")
        self.thread.start()

    def accept(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if hasattr(socket, "SO_PEERCRED"):
                _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            elif hasattr(socket, "LOCAL_PEERCRED"):
                credentials = connection.getsockopt(getattr(socket, "SOL_LOCAL", 0), socket.LOCAL_PEERCRED, 20)
                _, uid = struct.unpack_from("=II", credentials)
            else:
                connection.close()
                continue
            if uid != os.getuid():
                connection.close()
                continue
            connection.settimeout(120.)
            self.connections.append(connection)
            handler = threading.Thread(target=self.handle, args=(connection,))
            self.handlers.append(handler)
            handler.start()

    def handle(self, connection):
        sequence = 0
        try:
            while not self.stopping.is_set():
                request = receive(connection, wait_for_request=True)
                request_id = request["request_id"]
                if type(request_id) is not int or request_id <= sequence:
                    raise ValueError("nonmonotonic request identity")
                sequence = request_id
                try:
                    if set(request) != {"request_id", "operation", "identity", "information", "seed"}:
                        raise ValueError("inference request fields differ")
                    info = InformationState.from_payload(request["information"])
                    reply = self.batch.submit(request["identity"], request["operation"], info, request["seed"]).result(timeout=120.)
                    send(connection, dict(ok=True, request_id=sequence, identity=request["identity"],
                                          information_sha256=info.sha256, **reply))
                except Exception as error:
                    send(connection, dict(ok=False, request_id=sequence, error=str(error)))
        except (EOFError, OSError, KeyError, ValueError):
            pass
        finally:
            connection.close()

    def close(self):
        self.stopping.set()
        self.socket.close()
        for connection in self.connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.thread.join(timeout=5.)
        for handler in self.handlers:
            handler.join(timeout=125.)
        self.batch.close()
        if self.thread.is_alive() or any(handler.is_alive() for handler in self.handlers):
            raise RuntimeError("inference server leaked a thread")
