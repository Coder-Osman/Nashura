from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import threading
import time

import torch

from native_runner.training.event_checkpoint import load_event_checkpoint


@dataclass
class FrozenPolicy:
    policy: object
    tensors: tuple
    versions: tuple

    def validate(self):
        if any(module.training for module in self.policy.model.modules()):
            raise ValueError("resident actor policy entered training mode")
        current = tuple(self.policy.model.parameters()) + tuple(self.policy.model.buffers())
        if (len(current) != len(self.tensors) or
                any(actual is not saved for actual, saved in zip(current, self.tensors)) or
                tuple(tensor._version for tensor in current) != self.versions or
                any(parameter.requires_grad for parameter in self.policy.model.parameters())):
            raise ValueError("resident actor policy weights changed")


class ResidentPolicyPool:
    def __init__(self, *, max_policies=2, loader=load_event_checkpoint):
        if type(max_policies) is not int or not 0 <= max_policies <= 8:
            raise ValueError("resident policy capacity must be between zero and eight")
        self.max_policies = max_policies
        self.loader = loader
        self.owner = (os.getpid(), threading.get_ident())
        self.entries = OrderedDict()
        self.profile = dict(loads=0, hits=0, hash_seconds=0., load_seconds=0.)

    def load(self, path, *, device, expected_sha256, expected_contract_sha256=None):
        if self.owner != (os.getpid(), threading.get_ident()):
            raise ValueError("resident policy pool belongs to another actor")
        if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64 or
                any(character not in "0123456789abcdef" for character in expected_sha256)):
            raise ValueError("resident policy requires an immutable checkpoint identity")
        path = Path(path).resolve(strict=True)
        started = time.perf_counter()
        with path.open("rb") as checkpoint:
            actual_sha256 = hashlib.file_digest(checkpoint, "sha256").hexdigest()
        self.profile["hash_seconds"] += time.perf_counter() - started
        if actual_sha256 != expected_sha256:
            raise ValueError("resident checkpoint content differs from the job")
        resolved_device = torch.device(device)
        if resolved_device.type == "cuda" and resolved_device.index is None:
            resolved_device = torch.device("cuda", torch.cuda.current_device())
        key = (actual_sha256, str(resolved_device))
        cached = self.entries.get(key)
        if cached is not None:
            cached.validate()
            policy = cached.policy
        else:
            started = time.perf_counter()
            devices = [resolved_device.index] if resolved_device.type == "cuda" else []
            with torch.random.fork_rng(devices=devices):
                policy = self.loader(path, device=resolved_device)
            self.profile["load_seconds"] += time.perf_counter() - started
            self.profile["loads"] += 1
            if policy.checkpoint_sha256 != expected_sha256:
                raise ValueError("resident checkpoint changed during loading")
            policy.model.eval().requires_grad_(False)
        if expected_contract_sha256 is not None and policy.contract_sha256 != expected_contract_sha256:
            raise ValueError("resident policy model contract differs from the job")
        if cached is not None:
            self.entries.move_to_end(key)
            self.profile["hits"] += 1
        elif self.max_policies:
            tensors = tuple(policy.model.parameters()) + tuple(policy.model.buffers())
            self.entries[key] = FrozenPolicy(policy, tensors, tuple(tensor._version for tensor in tensors))
            while len(self.entries) > self.max_policies:
                self.entries.popitem(last=False)
        return policy

    def clear(self):
        self.entries.clear()
