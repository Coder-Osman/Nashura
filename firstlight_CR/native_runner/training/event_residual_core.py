"""Stateless gated core with identity-initialized residual capacity."""
from __future__ import annotations

import torch
from torch import nn


LEGACY_DECODER_ATTRIBUTES = (
    "no_target_embedding", "gate_head", "decoder_initial", "cross_attention",
    "decoder_norm", "candidate_query", "candidate_key", "location_condition",
    "location_film", "location_key", "location_query", "delay_offset_head",
    "delay_offset_embedding", "action_embedding", "decoder_step", "continue_head",
)


def active_gate_rows(hidden_size: int, *, device=None):
    """PyTorch LSTM order is input, forget, candidate, output."""
    return torch.cat((torch.arange(hidden_size, device=device),
                      torch.arange(2 * hidden_size, 4 * hidden_size, device=device)))


class ResidualMlpBlock(nn.Module):
    def __init__(self, width: int, hidden: int):
        super().__init__()
        self.norm = nn.LayerNorm(width)
        self.input = nn.Linear(width, hidden)
        self.output = nn.Linear(hidden, width)
        nn.init.zeros_(self.output.weight)
        nn.init.zeros_(self.output.bias)

    def forward(self, x):
        return x + self.output(torch.nn.functional.silu(self.input(self.norm(x))))


class StatelessGatedResidualCore(nn.Module):
    """Retain the zero-state LSTM function; never consume recurrent state.

    The two surviving biases remain separate so AdamW moments can migrate by
    slicing, without pretending merged-parameter moments are equivalent.
    """
    def __init__(self, source: nn.LSTMCell, *, blocks: int, residual_hidden: int):
        super().__init__()
        rows = active_gate_rows(source.hidden_size, device=source.weight_ih.device)
        self.weight_ih = nn.Parameter(source.weight_ih.detach()[rows].clone())
        self.bias_ih = nn.Parameter(source.bias_ih.detach()[rows].clone())
        self.bias_hh = nn.Parameter(source.bias_hh.detach()[rows].clone())
        self.residual = nn.Sequential(*(
            ResidualMlpBlock(source.hidden_size, residual_hidden) for _ in range(blocks)
        ))

    def forward(self, x, state):
        i, g, o = (torch.nn.functional.linear(x, self.weight_ih, self.bias_ih)
                   + self.bias_hh).chunk(3, dim=-1)
        cell = i.sigmoid() * g.tanh()
        hidden = self.residual(o.sigmoid() * cell.tanh())
        # Both outputs are current-input features; the next call ignores state.
        return hidden, cell


def install_stateless_residual_core(backbone, *, blocks: int, hidden: int):
    if not backbone.config.snapshot_only:
        raise ValueError("stateless residual event core requires snapshot_only")
    backbone.lstm_core = StatelessGatedResidualCore(
        backbone.lstm_core, blocks=blocks, residual_hidden=hidden
    )
    for name in LEGACY_DECODER_ATTRIBUTES:
        delattr(backbone, name)
