"""Old reward auditing keeps ownership, cancelled-row and evidence checks."""
import pytest

from native_runner.tests.test_event_tower_rewards import fixture
from native_runner.training.event_rewards import STRONG_TOWER_OVERFLOW_REWARD_CONFIG as OLD
from native_runner.training.event_shard_cache import VerifiedShardCache
from tools.event_rl_round_audit import audit_match, evidence_digest


def saved(tmp_path, *, owner=0):
    recorder = fixture(config=OLD)
    recorder.to_shard(tmp_path/'part-0.crev.zst', shard_id='tower', contract={}, owner=0)
    (tmp_path/'collector.log').write_text('{}\n')
    return recorder, dict(kind='recent', learner_owner=owner, game_id='tower'), dict(reward_config=OLD)


def test_old_reward_needs_no_settlement_file_and_keeps_cancelled_filter(tmp_path):
    _, match, plan = saved(tmp_path)
    result = audit_match(tmp_path, match, plan, VerifiedShardCache())
    assert result['totals']['lanes'] == 1
    assert result['totals']['effective_decisions'] == 2
    assert result['totals']['cancelled_decisions'] == 2
    assert result['components']['tower_telescoping_error_max'] < 1e-8
    assert result['evidence'] == evidence_digest(tmp_path, settlement=False)
    with pytest.raises(FileNotFoundError):
        evidence_digest(tmp_path, settlement=True)


def test_old_reward_rejects_wrong_owner_and_mixed_reward(tmp_path):
    _, match, plan = saved(tmp_path, owner=1)
    with pytest.raises(ValueError, match='owner or reward'):
        audit_match(tmp_path, match, plan, VerifiedShardCache())
    match['learner_owner'] = 0
    from native_runner.training.event_rewards import TOWER_OVERFLOW_REWARD_CONFIG
    from tools.event_rl_round_audit import audit_parsed_match
    shard = VerifiedShardCache().load(tmp_path/'part-0.json')
    with pytest.raises(ValueError, match='owner or reward'):
        audit_parsed_match(tmp_path, match, plan, shard.records,
                           dict(reward_config=TOWER_OVERFLOW_REWARD_CONFIG), {})


def test_old_reward_rejects_mutated_evidence(tmp_path):
    _, match, plan = saved(tmp_path)
    from tools.event_rl_round_audit import audit_parsed_match
    shard = VerifiedShardCache().load(tmp_path/'part-0.json')
    evidence = evidence_digest(tmp_path, settlement=False)
    (tmp_path/'collector.log').write_text('{"mutated": true}\n')
    with pytest.raises(ValueError, match='evidence changed'):
        audit_parsed_match(tmp_path, match, plan, shard.records, shard.manifest['contract'], evidence)
