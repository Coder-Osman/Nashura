"""Source-aligned explanations of the unchanged strict preflight gate."""

from dataclasses import replace
import json

import pytest
import torch

from native_runner.tests.test_event_preflight import rollout, rewrite
from native_runner.training.event_preflight import (
    EventPreflightError, load_complete_rollout, preflight_event_rollout,
)


def test_stored_joint_outlier_identifies_source_and_thresholds(rollout):
    model, summary = rollout
    rewrite(summary, lambda rows: rows[:-1] + [replace(rows[-1], log_prob=rows[-1].log_prob - .04)])
    metadata = []
    lanes, counts = load_complete_rollout([summary], row_metadata=metadata)
    assert counts["action_constraints"] is None
    assert counts["reward_config"] is None
    assert counts["reward_components"]["overflow_penalty"] == 0.
    assert all(isinstance(value, int) for key, value in counts.items()
               if key not in {"action_constraints", "execution_status_counts", "execution_credit_contract",
                              "reward_config", "reward_components"})
    with pytest.raises(EventPreflightError) as error:
        preflight_event_rollout(model, lanes, row_metadata=metadata, chunk_steps=2,
                                diagnostic_top_rows=2)
    metrics = error.value.metrics
    joint = metrics['diagnostics']['joint']
    worst = joint['top_rows'][0]
    assert len(joint['top_rows']) == 2
    assert worst['match_id'] == 'real-container'
    assert worst['owner'] == 0 and worst['observation_tick'] == 140
    assert worst['summary_path'] == str(summary)
    assert worst['shard_row_index'] == worst['row_index'] == 4
    assert worst['lane_index'] == 0
    assert worst['abs_log_ratio'] == metrics['max_abs_log_ratio']
    assert worst['log_ratio'] == pytest.approx(.04, abs=.001)
    assert worst['exceeds_max_tolerance']
    assert joint['rows_above_max_threshold'] == 1
    assert joint['rows_above_mean_threshold'] == 1
    assert joint['clipped_rows'] == 0
    assert joint['abs_difference_quantiles']['p99'] > joint['abs_difference_quantiles']['p50']
    assert set(worst['heads']) == {'gate', 'card', 'cell', 'delay'}
    json.dumps(metrics, allow_nan=False)


def test_head_decomposition_is_explained_without_changing_gate(rollout):
    model, summary = rollout
    rewrite(summary, lambda rows: rows[:-1] + [replace(
        rows[-1], card_log_prob=rows[-1].card_log_prob + .05,
        delay_log_prob=rows[-1].delay_log_prob - .05,
    )])
    metadata = []
    lanes, _ = load_complete_rollout([summary], row_metadata=metadata)
    stats = preflight_event_rollout(model, lanes, row_metadata=metadata, chunk_steps=2)
    assert stats.passed
    heads = stats.diagnostics['heads']
    for head, expected in [('card', -.05), ('delay', .05)]:
        assert heads[head]['available_rows'] == 5
        assert heads[head]['rows_above_max_threshold'] == 1
        worst = heads[head]['top_rows'][0]
        assert worst['observation_tick'] == 140
        assert worst['difference'] == pytest.approx(expected, abs=.001)
    assert stats.diagnostics['head_thresholds_diagnostic_only']
    json.dumps(stats.as_dict(), allow_nan=False)


def test_legacy_lane_without_metadata_reports_joint_indices(rollout):
    model, summary = rollout
    lanes, _ = load_complete_rollout([summary])
    old = lanes[0].old_log_prob.clone()
    old[2] -= .04
    with pytest.raises(EventPreflightError) as error:
        preflight_event_rollout(model, [replace(lanes[0], old_log_prob=old)])
    diagnostics = error.value.metrics['diagnostics']
    worst = diagnostics['joint']['top_rows'][0]
    assert worst['row_index'] == 2 and worst['lane_index'] == 0
    assert worst['match_id'] is None and worst['heads'] == {}
    assert all(head['missing_rows'] == 5 for head in diagnostics['heads'].values())
    json.dumps(error.value.metrics, allow_nan=False)


def test_nonfinite_diagnostics_remain_strict_json(rollout):
    model, summary = rollout
    metadata = []
    lanes, _ = load_complete_rollout([summary], row_metadata=metadata)
    old = lanes[0].old_log_prob.clone()
    old[-1] = torch.nan
    with pytest.raises(EventPreflightError) as error:
        preflight_event_rollout(model, [replace(lanes[0], old_log_prob=old)], row_metadata=metadata)
    joint = error.value.metrics['diagnostics']['joint']
    assert joint['nonfinite_rows'] == 1 and joint['finite_rows'] == 4
    assert joint['top_rows'][0]['old_log_prob'] is None
    assert joint['top_rows'][0]['observation_tick'] == 140
    json.dumps(error.value.metrics, allow_nan=False)


def test_metadata_tracks_owner_major_assembly_across_shards(rollout):
    model, summary = rollout
    rewrite(summary, lambda rows: [replace(row, owner=1 if i % 2 == 0 else 0,
                                           done=i in (3, 4))
                                   for i, row in enumerate(rows)])
    metadata = [[{'stale': True}]]
    lanes, counts = load_complete_rollout([summary, summary], row_metadata=metadata)
    assert len(lanes) == len(metadata) == 4
    assert counts['rows'] == 10
    assert [[row['shard_row_index'] for row in lane] for lane in metadata] == [
        [1, 3], [0, 2, 4], [1, 3], [0, 2, 4],
    ]
    assert [lane[0]['owner'] for lane in metadata] == [0, 1, 0, 1]
    assert preflight_event_rollout(model, lanes, row_metadata=metadata, chunk_steps=1).passed


def test_metadata_alignment_is_required(rollout):
    model, summary = rollout
    lanes, _ = load_complete_rollout([summary])
    with pytest.raises(ValueError, match='metadata must align'):
        preflight_event_rollout(model, lanes, row_metadata=[[]])
