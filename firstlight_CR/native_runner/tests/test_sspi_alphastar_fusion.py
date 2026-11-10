"""Engineering checks for hierarchical SSPI cross-branch fusion."""

from copy import deepcopy
from dataclasses import fields, replace

import pytest
import torch

from native_runner.tests.test_sspi_search_performance import _real_information
from native_runner.tests.test_sspi_tower_counterfactual import _information_and_target
from native_runner.training.fair_search.contracts import semantic_digest
from native_runner.training.az_joint.model import ChildGroupContextFusion
from native_runner.training.sspi.learning import create_optimizer, load_checkpoint, save_checkpoint
from native_runner.training.sspi.model import initialize_fresh_model
from native_runner.training.sspi.protocol import SSPIAction


@pytest.fixture(autouse=True)
def one_thread():
    torch.set_num_threads(1)


def _nonempty_information():
    information, _ = _information_and_target()
    frame = information.frame
    groups = frame.groups
    group_mask = groups.mask.clone()
    group_mask[0, 0] = True
    source_card = groups.source_card_vocab_id.clone()
    source_card[0, 0] = frame.own_cards.card_vocab_id[0, 0]
    group_type = groups.group_type.clone()
    group_type[0, 0] = 0
    owner_type = groups.owner_type.clone()
    owner_type[0, 0] = 0
    runtime_form = groups.runtime_form.clone()
    runtime_form[0, 0] = 0
    group_position = groups.position.clone()
    group_position[0, 0] = torch.tensor([5., 8.])
    group_extent = groups.extent.clone()
    group_extent[0, 0] = torch.tensor([4., 7., 6., 9.])
    groups = replace(groups, source_card_vocab_id=source_card, group_type=group_type,
        owner_type=owner_type, runtime_form=runtime_form, position=group_position,
        extent=group_extent, mask=group_mask)

    child_mask = groups.child_mask.clone()
    child_mask[0, 0] = True
    child_group_index = groups.child_group_index.clone()
    child_group_index[0, 0] = 0
    child_archetype_id = groups.child_archetype_id.clone()
    child_archetype_id[0, 0] = 1
    child_type = groups.child_type.clone()
    child_type[0, 0] = 0
    child_position = groups.child_position.clone()
    child_position[0, 0] = torch.tensor([5., 8.])
    child_extent = groups.child_extent.clone()
    child_extent[0, 0] = torch.tensor([4.5, 7.5, 5.5, 8.5])
    groups = replace(groups, child_mask=child_mask, child_group_index=child_group_index,
        child_archetype_id=child_archetype_id, child_type=child_type,
        child_position=child_position, child_extent=child_extent)
    return replace(information, frame=replace(frame, groups=groups))


def test_child_group_context_scatter_respects_indices_padding_and_gradients():
    torch.manual_seed(5)
    fusion = ChildGroupContextFusion(child_dim=8, group_dim=12)
    children = torch.randn(1, 3, 8, requires_grad=True)
    groups = torch.randn(1, 2, 12, requires_grad=True)
    indices = torch.tensor([[1, 0, -1]])
    mask = torch.tensor([[True, True, False]])
    output = fusion(children, groups, indices, mask)
    changed = groups.detach().clone()
    changed[:, 0] += 2.
    changed_output = fusion(children, changed, indices, mask)
    assert torch.equal(output[:, 0], changed_output[:, 0])
    assert not torch.equal(output[:, 1], changed_output[:, 1])
    assert torch.equal(output[:, 2], torch.zeros_like(output[:, 2]))
    output[:, :2].square().sum().backward()
    assert groups.grad is not None and bool(groups.grad.abs().sum() > 0)
    assert children.grad is not None and bool(children.grad.abs().sum() > 0)


def test_tower_value_context_is_row_order_invariant_and_reads_hp_and_position():
    model = initialize_fresh_model(20261103).eval()
    information, _ = _information_and_target()
    towers = information.frame.towers
    assert int(towers.mask.sum()) == 6
    permutation = torch.tensor([4, 2, 0, 5, 1, 3])
    reordered = replace(towers, **{
        item.name: getattr(towers, item.name)[:, permutation]
        for item in fields(towers) if isinstance(getattr(towers, item.name), torch.Tensor)
    })
    baseline = model.encoder.tower_value_encoder(towers)
    torch.testing.assert_close(model.encoder.tower_value_encoder(reordered), baseline, atol=1e-6, rtol=1e-6)

    hp_features = towers.features.clone()
    hp_features[0, 0, 0] *= .5
    changed_hp = replace(towers, features=hp_features)
    assert not torch.allclose(model.encoder.tower_value_encoder(changed_hp), baseline)
    positions = towers.position.clone()
    positions[0, 0, 0] += 3.
    changed_position = replace(towers, position=positions)
    assert not torch.allclose(model.encoder.tower_value_encoder(changed_position), baseline)


def test_policy_and_value_gradients_follow_their_fusion_paths(monkeypatch):
    model = initialize_fresh_model(20261103).train()
    information = _nonempty_information()
    captured = {}
    original_forward = model.encoder.candidate_encoder.forward

    def capture_candidate(*args, **kwargs):
        captured["child_memory"] = kwargs["child_memory"]
        return original_forward(*args, **kwargs)

    monkeypatch.setattr(model.encoder.candidate_encoder, "forward", capture_candidate)
    shared, encoded, _ = model.encode(information)
    torch.testing.assert_close(captured["child_memory"], encoded.contextual_child_memory)
    assert int(information.frame.groups.child_mask.sum()) == 1

    policies, values = model(information, [information.legality])
    assert model.parameter_count < 10_000_000
    assert values.shape == (1, 2) and bool(torch.isfinite(values).all())
    probabilities = policies[0].conditionals().exp()
    torch.testing.assert_close(probabilities.sum(), torch.tensor(1., device=probabilities.device))
    assert probabilities[2] == 0.
    legal = information.legality.plays.nonzero(as_tuple=False)
    card_row, cell, delay = map(int, legal[0])
    action = SSPIAction.play(information.legality.card_ids[card_row], cell, delay)
    (-policies[0].log_prob(action)).backward()
    for prefix in (
        "encoder.child_context_fusion.group_projection",
        "encoder.spatial_encoder.blocks.0.film",
        "policy_head.scalar_context",
        "policy_head.hand_context",
    ):
        gradients = [parameter.grad for name, parameter in model.named_parameters()
            if name == prefix or name.startswith(prefix + ".")]
        assert gradients and any(gradient is not None and bool(gradient.abs().sum() > 0)
            for gradient in gradients), prefix

    model.zero_grad(set_to_none=True)
    torch.nn.functional.cross_entropy(model.value_only(information), torch.tensor([0])).backward()
    for prefix in ("encoder.tower_value_encoder", "value_head"):
        gradients = [parameter.grad for name, parameter in model.named_parameters()
            if name == prefix or name.startswith(prefix + ".")]
        assert gradients and any(gradient is not None and bool(gradient.abs().sum() > 0)
            for gradient in gradients), prefix
    assert all(parameter.grad is None for parameter in model.policy_head.parameters())


def test_v4_checkpoint_roundtrip_and_strict_v3_rejection(tmp_path):
    model = initialize_fresh_model(20261103).eval()
    assert model.schema == "sspi-policy-value.v4"
    assert model.architecture["fusion_schema"] == "alphastar-hierarchical-cross-branch.v1"
    optimizer = create_optimizer(model)
    path = tmp_path / "fusion-v4.pt"
    identity = {"run": "fusion-checkpoint-test", "architecture": model.architecture_sha}
    save_checkpoint(path, model, optimizer, identity=identity, optimizer_steps=0)

    restored = initialize_fresh_model(20261103).eval()
    restored_optimizer = create_optimizer(restored)
    loaded = load_checkpoint(path, restored, restored_optimizer, expected_identity=identity)
    assert loaded["model_sha"] == semantic_digest(model.state_dict())
    assert restored.architecture_sha == model.architecture_sha
    assert semantic_digest((model.state_dict(), optimizer.state_dict())) == semantic_digest(
        (restored.state_dict(), restored_optimizer.state_dict()))

    before = semantic_digest(restored.state_dict())
    envelope = torch.load(path, weights_only=False)
    old = deepcopy(envelope)
    old["schema"] = "sspi-mac-pilot-checkpoint.v3"
    old["payload"]["schema"] = "sspi-mac-pilot-checkpoint.v3"
    old["payload"]["architecture"]["schema"] = "sspi-policy-value.v3"
    old["payload"]["architecture_sha"] = semantic_digest(old["payload"]["architecture"])
    old["payload_sha"] = semantic_digest(old["payload"])
    torch.save(old, path)
    with pytest.raises(ValueError, match="checkpoint"):
        load_checkpoint(path, restored, restored_optimizer, expected_identity=identity)
    assert semantic_digest(restored.state_dict()) == before
