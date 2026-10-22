import torch

from ..event_action_space import legal_event_actions
from ..v4.catalog import FIRST_REAL_CARD_VOCAB_ID
from native_runner.contracts import ObservationTier
from .policy import JointPolicy, LegalPrefixes


DELAY_MS = tuple(range(0, 3001, 100))
ADAPTER_ID = "source-a-old61-new30-prefix-normalized-native-payment.v1"


def native_payment_mask(*, tick, elixir_raw, cost_raw, timeline):
    if any(type(value) is not int or value < 0 for value in (tick, elixir_raw, cost_raw)):
        raise ValueError("native affordability requires nonnegative integer resource values")
    generated = [0]
    for offset in range(59):
        rate = 5_000_000 // timeline.elixir_rate(tick + offset).full_bar_ms
        generated.append(generated[-1] + rate)
    return torch.tensor([False] + [elixir_raw + generated[2 * slot - 1] >= cost_raw
        for slot in range(1, 31)], dtype=torch.bool)


def stable_card_rows(frame, catalog, card_ids, *, index=0):
    positions = {}
    for row, vocab_id in enumerate(frame.own_cards.card_vocab_id[index].tolist()):
        if not bool(frame.own_cards.mask[index, row]):
            continue
        offset = vocab_id - FIRST_REAL_CARD_VOCAB_ID
        if 0 <= offset < len(catalog.raw_card_ids):
            card_id = int(catalog.raw_card_ids[offset])
            if card_id in positions:
                raise ValueError("ambiguous stable card identity in semantic frame")
            positions[card_id] = row
    if any(card_id not in positions for card_id in card_ids):
        raise ValueError("legal card identity is missing from semantic frame")
    return tuple(positions[card_id] for card_id in card_ids)


def legal_prefixes(environment, observation, frame, owner, *, catalog):
    if (observation.owner != owner or observation.tier != ObservationTier.FAIR
            or observation.tick != environment.current_tick):
        raise ValueError("legality requires this owner's current FAIR observation")
    count = frame.own_cards.card_vocab_id.shape[1]
    legacy = legal_event_actions(environment, observation, frame, owner, catalog=catalog,
        card_count=count, cell_count=576, board_width=18, board_height=32, delay_offset_ms=DELAY_MS)
    if not legacy.decision_ready:
        raise ValueError("legality requested outside a decision boundary")
    rows = [row for row in range(count) if bool(frame.own_cards.mask[0, row]) and legacy.row_card_id[row] > 0]
    cards = [int(legacy.row_card_id[row]) for row in rows]
    plays = legacy.cell_masks[rows, :, None] & legacy.delay_masks[rows, None, :]
    raw = environment.raw_observation
    player = next(player for player in raw["players"] if player["owner"] == owner)
    costs = {int(card["cardId"]): int(card["cost"]) * 10000 for card in player["hand"]}
    for index, card_id in enumerate(cards):
        if card_id in costs:
            plays[index] &= native_payment_mask(tick=int(raw["tick"]), elixir_raw=int(player["elixirRaw"]),
                cost_raw=costs[card_id], timeline=environment.mode_timeline.timeline)[None, :]
    return LegalPrefixes(cards, plays)


def source_policy(source, frame, legality):
    policy = source.policy
    if tuple(policy.event_config.delay_offset_ms) != tuple(range(0, 6001, 100)):
        raise ValueError("source adapter requires the audited 61-bin delay vocabulary")
    if policy.config.ability_actions or not policy.backbone.config.snapshot_only:
        raise ValueError("source adapter requires stateless card-only A")
    device = next(policy.parameters()).device
    frame = frame.to_model_input(device, float_dtype=torch.float32)
    if frame.batch_size != 1:
        raise ValueError("source adapter takes one FAIR decision")
    rows = stable_card_rows(frame, policy.backbone.catalog, legality.card_ids)
    context = policy.encode(frame, policy.initial_state(1, device=device),
        episode_start=torch.ones(1, dtype=torch.bool, device=device))

    def logits(prefix):
        if not prefix:
            return policy.gate_head(context.policy_context)[0]
        if prefix == (1,):
            return policy.card_logits(context)[0, list(rows)]
        card_row = torch.tensor([rows[prefix[1]]], device=device)
        if len(prefix) == 2:
            return policy.cell_logits(context, card_row)[0]
        cell = torch.tensor([prefix[2]], device=device)
        return policy.delay_logits(context, card_row, cell)[0, :31]

    return JointPolicy(logits, legality), source.wdl_head(context.value_context)
