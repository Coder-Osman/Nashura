import pytest
import torch

from native_runner.tests.test_fair_final_learning import fixture_source
from native_runner.training.fair_final.model import ConditionalPolicyValue


def test_inherited_encoder_is_proven_and_unmapped_heads_are_reported():
    source, bundle = fixture_source()
    student = ConditionalPolicyValue(bundle=bundle, encoder_config=source.policy.backbone.config)
    mapping, report = student.copy_encoder_with_audit(source)
    assert mapping and report["mapped_tensors"] == len(mapping)
    assert 0 < report["student_parameter_coverage"] < 1
    assert 0 < report["source_parameter_coverage"] <= 1
    assert report["policy_head_copied_elements"] == 0
    assert report["wdl_head_copied_elements"] == 0
    assert report["policy_head_parameter_elements"] > 0
    assert report["wdl_head_parameter_elements"] > 0
    assert all(name.startswith("encoder.") for name in mapping)
    assert student.inheritance_report(source, mapping) == report
    first_target = next(name for name in mapping if name in dict(student.named_parameters()))
    with torch.no_grad():
        dict(student.named_parameters())[first_target].add_(0.1)
    with pytest.raises(ValueError, match="exact copy"):
        student.inheritance_report(source, mapping)
