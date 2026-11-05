"""Controls for the offline historical critic experiment, without private data."""
from dataclasses import replace

import pytest
import torch

from native_runner.tests.test_sspi_tower_counterfactual import _information_and_target
from native_runner.training import event_value_sensitivity as diagnostic
from native_runner.training.event_checkpoint import _build_model
from native_runner.training.event_config import EventPolicyConfigV1
from native_runner.training.event_model import EventModelConfigV1
from native_runner.training.v4.config import scaled_model_config_v4


@pytest.fixture
def frame():
    return _information_and_target()[0].frame


def test_only_selected_tower_hp_changes_and_original_is_immutable(frame):
    before = diagnostic.tensor_sha(diagnostic.tensor_fields(frame))
    changed = diagnostic.scale_tower_hp(frame, "enemy_princess", .5)
    diagnostic.verify_intervention(frame, changed)
    assert diagnostic.tensor_sha(diagnostic.tensor_fields(frame)) == before
    mask = diagnostic.tower_mask(frame, "enemy_princess")
    assert int(mask.sum()) == 2
    for index in (0, 5):
        torch.testing.assert_close(changed.towers.features[..., index][mask], frame.towers.features[..., index][mask] / 2)
        assert torch.equal(changed.towers.features[..., index][~mask], frame.towers.features[..., index][~mask])
    diagnostic.validate_frame(changed, scaled_model_config_v4(.125))
    assert diagnostic.tensor_sha(diagnostic.tensor_fields(diagnostic.scale_tower_hp(frame, "own_all", 1))) == before


def test_padding_and_destroyed_towers_remain_unchanged(frame):
    features, mask = frame.towers.features.clone(), frame.towers.mask.clone()
    features[0, 1, [0, 5]] = 0
    mask[0, 2] = False
    original = replace(frame, towers=replace(frame.towers, features=features, mask=mask))
    changed = diagnostic.scale_tower_hp(original, "own_all", .1)
    assert torch.equal(original.towers.features[0, 1:3], changed.towers.features[0, 1:3])
    assert diagnostic.tower_mask(original, "own_all").sum() == 1


@pytest.mark.parametrize("retention", [0., -1., 1.01, float("nan"), float("inf")])
def test_rejects_destruction_healing_and_nonfinite_retention(frame, retention):
    with pytest.raises(ValueError, match="retention"):
        diagnostic.scale_tower_hp(frame, "own_all", retention)


def test_potential_owner_reversal_and_damage_direction(frame):
    opposite = replace(frame, towers=replace(frame.towers, owner_type=1 - frame.towers.owner_type))
    assert diagnostic.potential(opposite) == pytest.approx(-diagnostic.potential(frame))
    assert diagnostic.potential(diagnostic.scale_tower_hp(frame, "own_all", .5)) < diagnostic.potential(frame)
    assert diagnostic.potential(diagnostic.scale_tower_hp(frame, "enemy_all", .5)) > diagnostic.potential(frame)


def test_non_hp_change_and_inconsistent_hp_are_detected(frame):
    corrupted = replace(frame, match_scalars=frame.match_scalars + 1.)
    with pytest.raises(AssertionError, match="non-HP"):
        diagnostic.verify_intervention(frame, corrupted)
    features = frame.towers.features.clone()
    features[..., 5] *= .5
    with pytest.raises(ValueError, match="inconsistent"):
        diagnostic.validate_frame(replace(frame, towers=replace(frame.towers, features=features)), scaled_model_config_v4(.125))


def test_real_network_input_gradient_matches_finite_difference(frame):
    torch.set_num_threads(1)
    torch.manual_seed(20261105)
    model = _build_model(replace(scaled_model_config_v4(.125), snapshot_only=True),
                         EventModelConfigV1(residual_blocks=2, residual_hidden=64), EventPolicyConfigV1())
    model.eval().requires_grad_(False)
    before = diagnostic.tensor_sha(list(model.named_parameters()) + list(model.named_buffers()))
    result = diagnostic.derivative_check(model, frame, "own_princess", epsilon=.002)
    assert abs(result["analytic"]) > 1e-7
    assert result["analytic"] == pytest.approx(result["finite_difference"], rel=.05, abs=1e-5)
    assert all(p.grad is None for p in model.parameters())
    assert diagnostic.tensor_sha(list(model.named_parameters()) + list(model.named_buffers())) == before


def test_summary_distinguishes_sensitivity_direction_and_flatness():
    row = {"sweeps": {}}
    for group, (owner, _) in diagnostic.GROUPS.items():
        row["sweeps"][group] = dict(changed_towers=2, potential=[0.] * 5,
                                    values=[0., -.1, -.2, -.3, -.4])
    summary = diagnostic.summarize([row])
    assert summary["own_princess"]["half_hp_direction_fraction"] == 1.
    assert summary["enemy_princess"]["half_hp_direction_fraction"] == 0.
    assert summary["enemy_princess"]["half_hp_sensitive_fraction"] == 1.
    row["sweeps"]["enemy_princess"]["values"] = [0.] * 5
    assert diagnostic.summarize([row])["enemy_princess"]["half_hp_sensitive_fraction"] == 0.
