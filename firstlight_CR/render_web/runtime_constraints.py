"""Backport only the delay constraint into a separately fingerprinted v2 runtime."""

import ast
from pathlib import Path

if __package__:
    from native_runner.training import event_delay_constraints as delay_policy
else:
    import event_delay_constraints as delay_policy

delay_constraints_contract = delay_policy.delay_constraints_contract
ACTION_SPACE = "native_runner/training/event_action_space.py"
DELAY_POLICY = "native_runner/training/event_delay_constraints.py"


def runtime_overlay(source):
    tree = ast.parse((source / ACTION_SPACE).read_text())
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "legal_event_actions"]
    if len(functions) != 1 or not isinstance(functions[0].body[-1], ast.Return):
        raise ValueError("unsupported frozen action-space implementation")
    function = functions[0]
    result = function.body[-1].value
    if not isinstance(result, ast.Call) or not isinstance(result.func, ast.Name) or result.func.id != "EventLegalActionsV1":
        raise ValueError("unsupported frozen legality result")
    # Preserve the frozen scheduler and affordability calculation. Intersect
    # before returning so gate/card selection sees the same filtered support.
    addition = ast.parse('''
from .event_delay_constraints import playable_delay_mask as policy_delay_mask
from ..match_factory import NATIVE_GAMEPLAY_END_TICK
policy_delays = torch.tensor(policy_delay_mask(
    delay_offset_ms, tick=int(getattr(observation, "tick", 0)),
    end_tick=NATIVE_GAMEPLAY_END_TICK), dtype=torch.bool, device=resolved_device)
delay_masks &= policy_delays
card_mask &= delay_masks.any(dim=-1)
cell_masks &= card_mask.unsqueeze(-1)
hand_index.masked_fill_(~card_mask, -1)
playable_now = any(bool(mask.hand_slots[int(hand_index[row])]) for row in range(card_count)
                   if bool(card_mask[row]))
''').body
    function.body[-1:-1] = addition
    ast.fix_missing_locations(tree)
    return {
        ACTION_SPACE: (ast.unparse(tree) + "\n").encode(),
        DELAY_POLICY: Path(delay_policy.__file__).read_bytes(),
    }
