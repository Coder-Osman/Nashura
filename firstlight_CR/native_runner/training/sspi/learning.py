"""Search-root-only SSPI policy/value losses and portable checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import hashlib
import os
from pathlib import Path
import random
import tempfile

import torch
import torch.nn.functional as F

from ..fair_search.contracts import semantic_digest, unpack
from .model import SSPIModel
from .protocol import (HandSlot, HandState, PendingRefill, SSPIAction,
    SSPILegality, SSPIInformation)


CHECKPOINT_SCHEMA = "sspi-mac-pilot-checkpoint.v2"
SEARCH_BUDGETS = {"A_5PCT_1024": 1024, "B_40PCT_128": 128}
EFFECTIVE_BATCH = 128
MICROBATCH = 4
ACCUMULATION_STEPS = EFFECTIVE_BATCH // MICROBATCH


class WDLLabel(IntEnum):
    WIN = 0
    DRAW = 1
    LOSS = 2


@dataclass(frozen=True)
class TrainingRow:
    frame: object
    legality: SSPILegality
    final_wdl: WDLLabel
    search_mode: str
    visits: tuple[tuple[SSPIAction, int], ...] = ()
    simulations: int = 0

    def __post_init__(self):
        if self.search_mode not in ("DIRECT", "SEARCH"):
            raise ValueError("training row mode must be DIRECT or SEARCH")
        if not isinstance(self.final_wdl, WDLLabel):
            object.__setattr__(self, "final_wdl", WDLLabel(self.final_wdl))
        if self.search_mode == "DIRECT":
            if self.visits or self.simulations:
                raise ValueError("DIRECT rows must not carry search targets")
            return
        if self.simulations not in SEARCH_BUDGETS.values() or not self.visits:
            raise ValueError("SEARCH rows require a complete fixed-budget root distribution")
        if (len({action.key for action, _ in self.visits}) != len(self.visits)
                or any(not self.legality.legal(action) or type(count) is not int or count <= 0
                    for action, count in self.visits)
                or sum(count for _, count in self.visits) != self.simulations):
            raise ValueError("SEARCH visits must be unique, legal, positive, and complete")


def terminal_wdl(owner: int, winner: int | None) -> WDLLabel:
    if type(owner) is not int or owner not in (0, 1) or winner not in (None, 0, 1):
        raise ValueError("terminal WDL requires a complete native winner result")
    if winner is None:
        return WDLLabel.DRAW
    return WDLLabel.WIN if winner == owner else WDLLabel.LOSS


def create_optimizer(model: SSPIModel):
    return torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=1e-4)


def search_root_loss(model: SSPIModel, rows: list[TrainingRow] | tuple[TrainingRow, ...]):
    """Compute both losses only after removing every DIRECT row."""
    if not rows:
        raise ValueError("training batch is empty")
    search_rows = [row for row in rows if row.search_mode == "SEARCH"]
    if not search_rows:
        raise ValueError("batch has no eligible SEARCH roots")
    policy_losses, value_logits, labels = [], [], []
    for row in search_rows:
        policies, logits = model(row.frame, [row.legality])
        policy = policies[0]
        policy_loss = -sum(policy.log_prob(action) * (count / row.simulations)
            for action, count in row.visits)
        policy_losses.append(policy_loss)
        value_logits.append(logits[0])
        labels.append(int(row.final_wdl))
    policy_loss = torch.stack(policy_losses).mean()
    logits = torch.stack(value_logits)
    targets = torch.tensor(labels, dtype=torch.long, device=logits.device)
    value_loss = F.cross_entropy(logits, targets)
    loss = policy_loss + value_loss
    if not bool(torch.isfinite(loss)):
        raise ValueError("SSPI policy/value loss is nonfinite")
    return dict(loss=loss, policy_loss=policy_loss, value_loss=value_loss,
        search_rows=len(search_rows), direct_rows=len(rows) - len(search_rows))


def train_search_step(model: SSPIModel, optimizer, rows, *, gradient_clip=1.):
    if type(optimizer) is not torch.optim.AdamW:
        raise TypeError("SSPI pilot requires AdamW")
    if gradient_clip != 1.:
        raise ValueError("SSPI gradient clipping is fixed at 1.0")
    optimizer.zero_grad(set_to_none=True)
    losses = search_root_loss(model, rows)
    losses["loss"].backward()
    norm = torch.nn.utils.clip_grad_norm_(model.parameters(), gradient_clip)
    if not bool(torch.isfinite(norm)):
        optimizer.zero_grad(set_to_none=True)
        raise ValueError("SSPI gradient norm is nonfinite")
    optimizer.step()
    losses["gradient_norm"] = float(norm.detach().cpu())
    losses["optimizer_steps"] = 1
    optimizer.zero_grad(set_to_none=True)
    return losses


def train_generation_step(model: SSPIModel, optimizer, rows):
    """Apply one fixed 128-root generation update as 32 four-root microbatches."""
    if len(rows) != EFFECTIVE_BATCH or any(row.search_mode != "SEARCH" for row in rows):
        raise ValueError("generation updates require exactly 128 SEARCH roots and no DIRECT rows")
    if type(optimizer) is not torch.optim.AdamW:
        raise TypeError("SSPI pilot requires AdamW")
    model.train()
    optimizer.zero_grad(set_to_none=True)
    policy_total = value_total = loss_total = 0.
    try:
        for offset in range(0, EFFECTIVE_BATCH, MICROBATCH):
            micro = search_root_loss(model, rows[offset:offset + MICROBATCH])
            (micro["loss"] / ACCUMULATION_STEPS).backward()
            loss_total += float(micro["loss"].detach().cpu()) / ACCUMULATION_STEPS
            policy_total += float(micro["policy_loss"].detach().cpu()) / ACCUMULATION_STEPS
            value_total += float(micro["value_loss"].detach().cpu()) / ACCUMULATION_STEPS
        norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
        if not bool(torch.isfinite(norm)):
            raise ValueError("SSPI generation gradient norm is nonfinite")
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
    except BaseException:
        optimizer.zero_grad(set_to_none=True)
        raise
    return dict(loss=loss_total, policy_loss=policy_total, value_loss=value_total,
        gradient_norm=float(norm.detach().cpu()), optimizer_steps=1,
        search_rows=EFFECTIVE_BATCH, direct_rows=0, microbatch=MICROBATCH,
        accumulation_steps=ACCUMULATION_STEPS)


def _action_from_key(key):
    fields = key.split(":")
    if fields == ["wait"]:
        return SSPIAction.wait()
    if len(fields) == 4 and fields[0] == "play":
        return SSPIAction.play(*(int(value) for value in fields[1:]))
    if len(fields) == 3 and fields[0] == "ability":
        return SSPIAction.ability(*(int(value) for value in fields[1:]))
    raise ValueError("serialized SSPI visit has an invalid tagged action key")


def load_episode_training_rows(directory):
    """Load only checksummed SEARCH roots from a naturally completed episode."""
    directory = Path(directory)
    manifest = torch.load(directory / "episode.pt", weights_only=True, map_location="cpu")
    manifest_path = directory / "episode.json"
    import json
    metadata = json.loads(manifest_path.read_text())
    checksum = hashlib.sha256((directory / "episode.pt").read_bytes()).hexdigest()
    if (metadata.get("schema") != "sspi-mac-pilot-episode.v1"
            or metadata.get("status") != "complete" or metadata.get("training_admissible") is not True
            or metadata.get("tensor_sha256") != checksum
            or manifest.get("schema") != metadata["schema"]
            or manifest.get("identity") != metadata.get("identity")):
        raise ValueError("SSPI episode is incomplete, quarantined, or checksum-invalid")
    rows = []
    for root in manifest.get("roots", ()):
        if root.get("simulations") not in SEARCH_BUDGETS.values():
            raise ValueError("SSPI episode contains an incomplete or unsupported search root")
        slots = tuple(HandSlot(**item) for item in root["hand"]["slots"])
        hand = HandState(deck=tuple(tuple(item) for item in root["hand"]["deck"]),
            slots=slots, known_next_card_id=root["hand"]["known_next_card_id"],
            cycle=tuple(tuple(item) for item in root["hand"]["cycle"]),
            pending_refills=tuple(PendingRefill(**item) for item in root["hand"]["pending_refills"]))
        legality = SSPILegality(root["card_ids"], root["plays"], wait=root["wait"])
        visits = tuple((_action_from_key(key), int(count)) for key, count in root["visits"])
        information = SSPIInformation(owner=root["owner"], tick=root["tick"],
            frame=unpack(root["frame"]), hand=hand, legality=legality)
        if information.sha256 != root["fair_sha256"]:
            raise ValueError("serialized SEARCH root FAIR digest changed")
        rows.append(TrainingRow(information, legality, WDLLabel[root["wdl"]], "SEARCH",
            visits=visits, simulations=root["simulations"]))
    if len(rows) != metadata.get("search_roots"):
        raise ValueError("SSPI episode search-root count differs from its metadata")
    return rows


def _optimizer_to(optimizer, device):
    for state in optimizer.state.values():
        for key, value in state.items():
            if isinstance(value, torch.Tensor):
                state[key] = value.to(device)


def save_checkpoint(path, model: SSPIModel, optimizer, *, identity, optimizer_steps: int, run_state=None):
    if (not isinstance(model, SSPIModel) or type(optimizer) is not torch.optim.AdamW
            or type(optimizer_steps) is not int or optimizer_steps < 0 or not isinstance(identity, dict)):
        raise ValueError("invalid SSPI checkpoint components")
    if any(parameter.grad is not None for parameter in model.parameters()):
        raise ValueError("SSPI checkpoints require a clean optimizer boundary")
    state = {name: tensor.detach().cpu().clone() for name, tensor in model.state_dict().items()}
    mps_rng = (torch.mps.get_rng_state().cpu() if torch.backends.mps.is_available() else None)
    run_state = {} if run_state is None else dict(run_state)
    payload = dict(schema=CHECKPOINT_SCHEMA, architecture=model.architecture,
        architecture_sha=model.architecture_sha, identity=dict(identity), identity_sha=semantic_digest(identity),
        model=state, model_sha=semantic_digest(state), optimizer=optimizer.state_dict(),
        optimizer_sha=semantic_digest(optimizer.state_dict()), optimizer_steps=optimizer_steps,
        python_rng=random.getstate(), cpu_rng=torch.get_rng_state(), mps_rng=mps_rng,
        run_state=run_state, run_state_sha=semantic_digest(run_state),
        model_training=model.training)
    envelope = dict(schema=CHECKPOINT_SCHEMA, payload=payload, payload_sha=semantic_digest(payload))
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + ".",
            suffix=".tmp", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        torch.save(envelope, temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
    os.replace(temporary_path, destination)
    return dict(path=str(destination), file_sha=hashlib.sha256(destination.read_bytes()).hexdigest(),
        model_sha=payload["model_sha"], architecture_sha=payload["architecture_sha"])


def load_checkpoint(path, model: SSPIModel, optimizer, *, expected_identity, device="cpu"):
    data = Path(path).read_bytes()
    envelope = torch.load(path, weights_only=True, map_location="cpu")
    if (not isinstance(envelope, dict) or envelope.get("schema") != CHECKPOINT_SCHEMA
            or semantic_digest(envelope.get("payload")) != envelope.get("payload_sha")):
        raise ValueError("SSPI checkpoint envelope checksum differs")
    payload = envelope["payload"]
    checks = (
        payload.get("schema") == CHECKPOINT_SCHEMA,
        payload.get("architecture_sha") == model.architecture_sha,
        payload.get("architecture") == model.architecture,
        payload.get("identity") == expected_identity,
        payload.get("identity_sha") == semantic_digest(expected_identity),
        payload.get("model_sha") == semantic_digest(payload.get("model")),
        payload.get("optimizer_sha") == semantic_digest(payload.get("optimizer")),
        payload.get("run_state_sha") == semantic_digest(payload.get("run_state")),
    )
    if not all(checks):
        raise ValueError("SSPI checkpoint model, identity, or optimizer checksum differs")
    model.load_state_dict(payload["model"], strict=True)
    model.to(device)
    optimizer.load_state_dict(payload["optimizer"])
    _optimizer_to(optimizer, device)
    random.setstate(payload["python_rng"])
    torch.set_rng_state(payload["cpu_rng"])
    if payload["mps_rng"] is not None:
        if not torch.backends.mps.is_available():
            raise RuntimeError("checkpoint contains MPS RNG state but MPS is unavailable")
        torch.mps.set_rng_state(payload["mps_rng"])
    model.train(bool(payload["model_training"]))
    return dict(file_sha=hashlib.sha256(data).hexdigest(), model_sha=payload["model_sha"],
        optimizer_steps=payload["optimizer_steps"], identity_sha=payload["identity_sha"],
        run_state=payload["run_state"])
