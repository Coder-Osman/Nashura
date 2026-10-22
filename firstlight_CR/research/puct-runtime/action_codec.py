"""Canonical event action keys without importing the native environment."""
from __future__ import annotations
import torch
from native_runner.training.event_model import EventActionV1

def action_key(action: EventActionV1) -> str:
    if not bool(action.act[0]):
        return "wait"
    if int(action.ability_candidate[0]) >= 0:
        return f"ability:{int(action.ability_candidate[0])}:{int(action.delay_bin[0])}"
    return f"play:{int(action.card_row[0])}:{int(action.cell[0])}:{int(action.delay_bin[0])}"


def decode_action(key: str, *, device: torch.device) -> EventActionV1:
    integer = lambda value: torch.tensor([value], dtype=torch.long, device=device)
    if key == "wait":
        return EventActionV1(torch.tensor([False], device=device), integer(-1), integer(-1), integer(-1))
    fields = key.split(":")
    if len(fields) == 4 and fields[0] == "play":
        row, cell, delay = map(int, fields[1:])
        if min(row, cell, delay) < 0:
            raise ValueError("negative play action coordinate")
        return EventActionV1(torch.tensor([True], device=device), integer(row), integer(cell), integer(delay))
    if len(fields) == 3 and fields[0] == "ability":
        source, delay = map(int, fields[1:])
        if source < 0 or delay < -1:
            raise ValueError("invalid ability action coordinate")
        return EventActionV1(torch.tensor([True], device=device), integer(-1), integer(-1),
                             integer(delay), integer(source))
    raise ValueError("invalid event search action")

