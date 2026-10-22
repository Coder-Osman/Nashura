"""Contract tests for the event-driven policy heads and IL loss."""

from __future__ import annotations

import pytest
import torch

from native_runner.training.event_cache import EventObservationRecordV1
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import (
    EventCardPolicyV1,
    EventModelConfigV1,
    build_event_targets,
    event_il_loss,
    pad_legacy_delay_head,
)
from native_runner.tests.test_training_v4_model import _batch, _catalog, _model


def _force_gate(model: EventCardPolicyV1, *, act: bool) -> None:
    """Make the gate deterministic so sampler tests do not depend on RNG."""

    with torch.no_grad():
        model.gate_head[-1].bias.copy_(torch.tensor([0.0, 12.0] if act else [12.0, 0.0]))


def _event_model() -> EventCardPolicyV1:
    return EventCardPolicyV1(_model())


def _record(*, owner: int, wait: bool, card_id: int | None, cell, delay_bin: int) -> EventObservationRecordV1:
    return EventObservationRecordV1(
        replay_tag="r",
        owner=owner,
        observation_tick=200,
        trigger_event_id="e",
        trigger_owner=1 - owner,
        trigger_card_id=7,
        wait=wait,
        card_id=card_id,
        hand_slot=0 if not wait else None,
        target_cell=cell,
        delay_ms=(delay_bin * 100) if not wait else 0,
        delay_bin=delay_bin,
        target_tick=200 + delay_bin * 2 if not wait else None,
        next_trigger_tick=None,
        terminal=False,
        candidate_index=0 if not wait else -1,
        candidate_uid=101 if not wait else -1,
        legal_at_trigger=not wait,
        card_in_hand=not wait,
    )


def test_event_forward_shapes() -> None:
    model = _event_model()
    model.eval()
    batch = _batch(2)
    state = model.initial_state(2)
    with torch.no_grad():
        output = model(batch, state, episode_start=torch.ones(2, dtype=torch.bool))
    assert tuple(output.gate_logits.shape) == (2, 2)
    assert tuple(output.card_logits.shape) == (2, 8)
    assert tuple(output.delay_logits.shape) == (2, EventPolicyConfigV1().delay_bin_count)
    assert tuple(output.cell_logits.shape) == (2, 18 * 32)


def test_event_targets_and_loss_backward() -> None:
    catalog = _catalog()
    model = _event_model()
    batch = _batch(2)
    records = [
        _record(owner=0, wait=False, card_id=catalog.raw_card_ids[0], cell=(3, 4), delay_bin=12),
        _record(owner=1, wait=True, card_id=None, cell=None, delay_bin=0),
    ]
    targets = build_event_targets(batch, records, catalog)
    assert targets.gate.tolist() == [1, 0]
    assert targets.card_row[0].item() >= 0
    assert targets.act_count == 1
    assert targets.cell_count == 1
    assert targets.cell[0].item() == 4 * 18 + 3

    state = model.initial_state(2)
    output = model(batch, state, episode_start=torch.ones(2, dtype=torch.bool), card_row=targets.card_row)
    total, parts = event_il_loss(
        output,
        targets,
        event_config=EventPolicyConfigV1(),
        delay_neighbor_weight=EventModelConfigV1().delay_neighbor_weight,
    )
    assert torch.isfinite(total)
    for name, value in parts.items():
        assert torch.isfinite(value), name
    total.backward()
    assert model.card_query.weight.grad is not None
    assert model.delay_head[-1].weight.grad is not None


def test_event_loss_coarse_delay_branch() -> None:
    catalog = _catalog()
    model = _event_model()
    batch = _batch(1)
    records = [_record(owner=0, wait=False, card_id=catalog.raw_card_ids[0], cell=(3, 4), delay_bin=12)]
    targets = build_event_targets(batch, records, catalog)
    state = model.initial_state(1)
    output = model(batch, state, episode_start=torch.ones(1, dtype=torch.bool), card_row=targets.card_row)
    total, parts = event_il_loss(
        output,
        targets,
        event_config=EventPolicyConfigV1(),
        delay_neighbor_weight=0.2,
        delay_coarse_weight=0.3,
        delay_coarse_group=5,
    )
    assert torch.isfinite(total)
    assert "delay_coarse" in parts and torch.isfinite(parts["delay_coarse"])
    assert float(parts["delay_coarse"]) > 0.0
    total.backward()


def test_event_targets_mask_out_of_deck_card() -> None:
    catalog = _catalog()
    batch = _batch(1)
    # 999999 is not in the deck; the row keeps only the gate loss.
    records = [_record(owner=0, wait=False, card_id=999999, cell=(1, 1), delay_bin=3)]
    targets = build_event_targets(batch, records, catalog)
    assert targets.gate.tolist() == [1]
    assert targets.card_row.item() == -1
    assert targets.act_count == 0


def test_place_head_is_conditioned_on_the_chosen_card() -> None:
    """Card and placement are a factorization, not two independent marginals.

    The cell and delay heads consume the embedding of the selected card, so the
    joint can express "this card goes here, that card goes there" instead of
    drawing a placement from a card-agnostic distribution. Card-first order also
    means the gate and card heads must be untouched by the conditioning input.
    This locks the invariant in: a refactor that decoupled the heads would fail.
    """

    model = _event_model()
    model.eval()
    batch = _batch(1)
    episode_start = torch.ones(1, dtype=torch.bool)

    def run(card_row: torch.Tensor):
        # A fresh state per call: the recurrent state is an input, not shared
        # mutation, so any difference below comes from the conditioning only.
        state = model.initial_state(1)
        with torch.no_grad():
            return model(batch, state, episode_start=episode_start, card_row=card_row)

    first_row = torch.zeros(1, dtype=torch.long)
    repeat = run(first_row)
    baseline = run(first_row)
    other = run(torch.ones(1, dtype=torch.long))

    # Control: the same conditioning input reproduces the same logits bitwise,
    # so a larger difference for another card cannot be run-to-run noise.
    for name in ("gate_logits", "card_logits", "cell_logits", "delay_logits"):
        assert torch.equal(getattr(repeat, name), getattr(baseline, name)), name

    # Upstream of the conditioning: card-first decoding is unaffected.
    assert torch.equal(baseline.gate_logits, other.gate_logits)
    assert torch.equal(baseline.card_logits, other.card_logits)

    # Downstream: both placement and timing actually read the selected card.
    assert not torch.allclose(baseline.cell_logits, other.cell_logits)
    assert not torch.allclose(baseline.delay_logits, other.delay_logits)


def _live_cell_columns(model: EventCardPolicyV1) -> None:
    """Give the delay head's cell columns weight so the coupling is live."""

    with torch.no_grad():
        model.delay_head[0].weight[:, -EventModelConfigV1().cell_hidden :].normal_(0.0, 0.02)


def _context(model: EventCardPolicyV1):
    batch = _batch(1)
    state = model.initial_state(1)
    episode_start = torch.ones(1, dtype=torch.bool)
    with torch.no_grad():
        return model.encode(batch, state, episode_start=episode_start)


def test_delay_head_conditions_on_the_selected_cell() -> None:
    """Timing belongs to the same decision as placement.

    The delay head reads the chosen cell, so "rush the bridge now" and "drop
    this at the back" no longer share one timing distribution.
    """

    model = _event_model()
    model.eval()
    _live_cell_columns(model)
    context = _context(model)
    card = torch.zeros(1, dtype=torch.long)
    with torch.no_grad():
        near = model.heads(context, card_row=card, cell=torch.tensor([100]))
        far = model.heads(context, card_row=card, cell=torch.tensor([500]))

    assert not torch.allclose(near.delay_logits, far.delay_logits)
    # Conditioning the delay must not perturb the heads upstream of it.
    assert torch.equal(near.gate_logits, far.gate_logits)
    assert torch.equal(near.card_logits, far.card_logits)
    assert torch.equal(near.cell_logits, far.cell_logits)


def test_fresh_delay_head_ignores_the_cell_input() -> None:
    """A fresh model reproduces the pre-cell-conditioning delay head exactly.

    The trailing cell columns start at zero, which is also what lets an older
    checkpoint load without changing its behaviour (see the migration test).
    """

    model = _event_model()
    model.eval()
    cell_hidden = EventModelConfigV1().cell_hidden
    assert float(model.delay_head[0].weight[:, -cell_hidden:].abs().sum()) == 0.0

    context = _context(model)
    card = torch.zeros(1, dtype=torch.long)
    with torch.no_grad():
        first = model.heads(context, card_row=card, cell=torch.tensor([7]))
        second = model.heads(context, card_row=card, cell=torch.tensor([401]))
    assert torch.equal(first.delay_logits, second.delay_logits)


def test_heads_honour_a_cell_mask_when_conditioning_the_delay() -> None:
    """Decoders resolve legality outside the model.

    Passing the mask in makes the model's own cell pick match the decoder's, so
    the delay is conditioned on the placement that will actually be played.
    """

    model = _event_model()
    model.eval()
    _live_cell_columns(model)
    context = _context(model)
    card = torch.zeros(1, dtype=torch.long)
    target = 500
    mask = torch.zeros(1, model.cell_count, dtype=torch.bool)
    mask[0, target] = True
    with torch.no_grad():
        masked = model.heads(context, card_row=card, cell_mask=mask)
        explicit = model.heads(context, card_row=card, cell=torch.tensor([target]))
    assert torch.equal(masked.delay_logits, explicit.delay_logits)


def test_legacy_delay_head_state_dict_is_padded() -> None:
    """Checkpoints trained before the cell conditioning still load.

    They lack the trailing columns; zero padding keeps their timing behaviour
    intact instead of failing the strict load outright.
    """

    model = _event_model()
    cell_hidden = EventModelConfigV1().cell_hidden
    legacy = {key: value.clone() for key, value in model.state_dict().items()}
    widened = legacy["delay_head.0.weight"].shape[1]
    assert widened > cell_hidden
    legacy["delay_head.0.weight"] = legacy["delay_head.0.weight"][:, : widened - cell_hidden]

    restored = _event_model()
    migrated = pad_legacy_delay_head(legacy, restored)
    restored.load_state_dict(migrated)

    # Old columns carried over verbatim, new ones zero.
    assert torch.equal(migrated["delay_head.0.weight"][:, : widened - cell_hidden], legacy["delay_head.0.weight"])
    assert float(migrated["delay_head.0.weight"][:, widened - cell_hidden :].abs().sum()) == 0.0

    # One shared batch, so any logit difference would come from the weights.
    batch = _batch(1)
    episode_start = torch.ones(1, dtype=torch.bool)
    card = torch.zeros(1, dtype=torch.long)
    with torch.no_grad():
        expected_context = model.encode(batch, model.initial_state(1), episode_start=episode_start)
        restored_context = restored.encode(batch, restored.initial_state(1), episode_start=episode_start)
        expected = model.heads(expected_context, card_row=card, cell=torch.tensor([9]))
        actual = restored.heads(restored_context, card_row=card, cell=torch.tensor([9]))
    assert torch.equal(actual.delay_logits, expected.delay_logits)

    # A shape that is not the one known migration is still rejected.
    with pytest.raises(ValueError):
        pad_legacy_delay_head({**legacy, "delay_head.0.weight": torch.zeros(3, widened)}, restored)


def test_select_action_respects_card_and_per_card_cell_masks() -> None:
    """Sampling is mask-respecting at every head, and the cell mask is the one
    belonging to the card that was actually drawn."""

    model = _event_model()
    model.eval()
    _force_gate(model, act=True)
    context = _context(model)

    card_count, cell_count = model.card_count, model.cell_count
    forced_card = 5
    legal_cells = (7, 11)
    card_mask = torch.zeros(1, card_count, dtype=torch.bool)
    card_mask[0, forced_card] = True
    # Only the forced card has a legal placement; the other cards get none, so a
    # wrongly gathered row would be empty and the sample would escape the mask.
    cell_masks = torch.zeros(1, card_count, cell_count, dtype=torch.bool)
    for cell in legal_cells:
        cell_masks[0, forced_card, cell] = True

    for _ in range(8):
        step = model.select_action(context, card_mask=card_mask, cell_masks=cell_masks)
        assert bool(step.action.act[0])
        assert int(step.action.card_row[0]) == forced_card
        assert int(step.action.cell[0]) in legal_cells
        assert 0 <= int(step.action.delay_bin[0]) < model.event_config.delay_bin_count


def test_joint_log_prob_is_the_sum_of_its_heads() -> None:
    model = _event_model()
    model.eval()
    _force_gate(model, act=True)
    context = _context(model)
    step = model.select_action(context)
    assert torch.allclose(
        step.log_prob,
        step.gate_log_prob + step.card_log_prob + step.cell_log_prob + step.delay_log_prob,
    )


def test_wait_rows_carry_no_head_terms() -> None:
    """Waiting makes no card, cell or delay decision, so those terms vanish."""

    model = _event_model()
    model.eval()
    _force_gate(model, act=False)
    context = _context(model)
    step = model.select_action(context)

    assert not bool(step.action.act[0])
    assert int(step.action.card_row[0]) == -1
    assert int(step.action.cell[0]) == -1
    assert int(step.action.delay_bin[0]) == -1
    for term in ("card_log_prob", "cell_log_prob", "delay_log_prob"):
        assert float(getattr(step, term).abs().sum()) == 0.0
    for term in ("card_entropy", "cell_entropy", "delay_entropy"):
        assert float(getattr(step, term).abs().sum()) == 0.0
    assert torch.allclose(step.log_prob, step.gate_log_prob)


def test_evaluate_actions_reproduces_the_behaviour_log_prob() -> None:
    """PPO's ratio is 1 on the collecting policy, including the cell conditioning
    (the stored cell and delay are re-scored under the stored card)."""

    model = _event_model()
    model.eval()
    _force_gate(model, act=True)
    context = _context(model)
    collected = model.select_action(context, temperatures=(1.0, 1.3, 0.7, 1.0))
    rescored = model.evaluate_actions(context, collected.action, temperatures=(1.0, 1.3, 0.7, 1.0))
    assert torch.allclose(rescored.log_prob, collected.log_prob)
    assert rescored.temperatures == (1.0, 1.3, 0.7, 1.0)


def test_temperature_is_recorded_and_changes_the_log_prob() -> None:
    model = _event_model()
    model.eval()
    _force_gate(model, act=True)
    context = _context(model)
    collected = model.select_action(context, temperatures=(1.0, 1.0, 1.0, 1.0))
    sharpened = model.evaluate_actions(context, collected.action, temperatures=(1.0, 0.5, 1.0, 1.0))
    assert sharpened.temperatures == (1.0, 0.5, 1.0, 1.0)
    assert not torch.allclose(sharpened.log_prob, collected.log_prob)

    with pytest.raises(ValueError):
        model.evaluate_actions(context, collected.action, temperatures=(1.0, 1.0, 1.0))
    with pytest.raises(ValueError):
        model.evaluate_actions(context, collected.action, temperatures=(1.0, 0.0, 1.0, 1.0))


def test_gate_mask_forbids_act_when_nothing_is_deliverable() -> None:
    """A short elixir bar does not forbid acting — the delay carries the play to
    when elixir arrives. But when no delay in the vocabulary reaches that point,
    ACT is not an available action and the gate must not be able to pick it."""

    model = _event_model()
    model.eval()
    _force_gate(model, act=True)          # logits lean ACT hard
    context = _context(model)
    blocked = torch.tensor([[True, False]], dtype=torch.bool)
    for _ in range(4):
        step = model.select_action(context, gate_mask=blocked)
        assert not bool(step.action.act[0])
        assert int(step.action.card_row[0]) == -1
        # Only the gate term is live on a WAIT row.
        assert float(step.log_prob[0]) == pytest.approx(float(step.gate_log_prob[0]), abs=1e-6)

    allowed = torch.tensor([[True, True]], dtype=torch.bool)
    assert bool(model.select_action(context, gate_mask=allowed).action.act[0])
