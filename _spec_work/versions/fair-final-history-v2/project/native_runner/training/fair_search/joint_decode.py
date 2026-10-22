import torch
from types import MethodType

from ..v4.model import _distribution
from ..v4.tensors import concatenate_tensor_records
from .learner import decode_action


def action_key(action):
    if not bool(action.act[0]):
        return "wait"
    return f"play:{int(action.card_row[0])}:{int(action.cell[0])}:{int(action.delay_bin[0])}"


@torch.no_grad()
def score_actions(policy, context, masks, actions):
    device = context.policy_context.device
    indices = torch.zeros(len(actions), dtype=torch.long, device=device)
    result = policy.evaluate_actions(context.index_select(indices),
        concatenate_tensor_records([decode_action(key, device) for key in actions]),
        **{name: mask.expand(len(actions), *mask.shape[1:]) for name, mask in masks.items()}, validate=True)
    return dict(zip(actions, result.log_prob.cpu().tolist()))


@torch.no_grad()
def joint_beam(policy, context, masks, *, width=32):
    if policy.config.ability_actions or context.policy_context.shape[0] != 1 or type(width) is not int or width < 1:
        raise ValueError("joint beam requires one card-only frame and positive width")
    device = context.policy_context.device
    gate_logits, gate_mask = policy.event_gate(context, masks["gate_mask"])
    gate = _distribution(gate_logits, mask=gate_mask, temperature=1., validate=True).logits[0]
    candidates = ["wait"]
    if bool(gate_mask[0, 1]):
        card_logs = _distribution(policy.card_logits(context), mask=masks["card_mask"], temperature=1., validate=True).logits[0]
        cards = torch.nonzero(masks["card_mask"][0], as_tuple=False).flatten().tolist()
        cards = sorted(cards, key=lambda card: (-float(card_logs[card]), str(card)))[:width]
        card_tensor = torch.tensor(cards, device=device)
        context_cards = context.index_select(torch.zeros(len(cards), dtype=torch.long, device=device))
        cell_masks = masks["cell_masks"][0, card_tensor]
        cell_logs = _distribution(policy.cell_logits(context_cards, card_tensor), mask=cell_masks,
                                  temperature=1., validate=True).logits
        prefixes = []
        for index, card in enumerate(cards):
            cells = torch.nonzero(cell_masks[index], as_tuple=False).flatten().tolist()
            scores = cell_logs[index].cpu().tolist()
            base = float(gate[1] + card_logs[card])
            prefixes.extend((base + scores[cell], card, cell) for cell in cells)
        prefixes = sorted(prefixes, key=lambda row: (-row[0], f"play:{row[1]}:{row[2]}:"))[:width]
        card_tensor = torch.tensor([row[1] for row in prefixes], device=device)
        cell_tensor = torch.tensor([row[2] for row in prefixes], device=device)
        context_cells = context.index_select(torch.zeros(len(prefixes), dtype=torch.long, device=device))
        delay_masks = masks["delay_mask"].expand(len(prefixes), -1, -1)
        delays = policy.action_delay_distribution(context_cells, card_tensor, cell_tensor, None,
                                                  delay_masks, 1., True).logits.cpu().tolist()
        complete = []
        for index, (prefix_score, card, cell) in enumerate(prefixes):
            legal = torch.nonzero(masks["delay_mask"][0, card], as_tuple=False).flatten().tolist()
            complete.extend((prefix_score + delays[index][delay], f"play:{card}:{cell}:{delay}") for delay in legal)
        candidates.extend(action for _, action in sorted(complete, key=lambda row: (-row[0], row[1]))[:width])
    logs = score_actions(policy, context, masks, candidates)
    return dict(action=min(logs, key=lambda key: (-logs[key], key)), log_probabilities=logs,
                beam_width=width, approximate=True)


def install_joint_beam(policy, *, width=32):
    original = policy.select_action

    def select(self, context, *, gate_mask=None, card_mask=None, cell_masks=None, delay_mask=None,
               temperatures=(1., 1., 1., 1.), sample=True, validate=False, sample_uniforms=None):
        masks = dict(gate_mask=gate_mask, card_mask=card_mask, cell_masks=cell_masks, delay_mask=delay_mask)
        if sample:
            return original(context, **masks, temperatures=temperatures, sample=sample,
                            validate=validate, sample_uniforms=sample_uniforms)
        if tuple(temperatures) != (1., 1., 1., 1.) or any(value is None for value in masks.values()):
            raise ValueError("joint beam requires original probabilities and explicit legal masks")
        decoded = joint_beam(self, context, masks, width=width)
        action = decode_action(decoded["action"], context.policy_context.device)
        return self.evaluate_actions(context, action, **masks, temperatures=temperatures, validate=True)

    policy.select_action = MethodType(select, policy)
