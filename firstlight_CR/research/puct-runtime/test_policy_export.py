import torch

from collect_policy_episode import export_completed_episode, opponent_identity
from native_runner.tests.test_event_rollout import _decision, _recorder
from native_runner.training.event_cache import default_event_contract, load_event_rl_shard


def test_completed_episode_checkpoint_exports_real_shard_and_summary(tmp_path):
    recorder = _recorder()
    _decision(recorder, 0, 200)
    recorder.advance(ticks=20, rewards={0: 1., 1: -1.}, done=True)
    recording = dict(recorder=recorder, contract=default_event_contract(), mode="on-policy",
                     episode=dict(ended=True, truncated=False))
    recovery = tmp_path / "completed-recorder.pt"
    torch.save(recording, recovery)
    # This is an artifact created by this test, not a user-supplied pickle.
    restored = torch.load(recovery, map_location="cpu", weights_only=False)
    export_completed_episode(restored, tmp_path)
    assert (tmp_path / "part-00000.crev.zst").is_file()
    shard = load_event_rl_shard(tmp_path / "part-00000.json", verify=True)
    assert len(shard.records) == 1 and shard.records[0].done
    assert shard.records[0].reward == 1.


def test_opponent_identity_uses_pinned_allocation():
    config = {"round_games": {"self_play": 72, "fixed_1250": 12, "recent": 24},
              "opponent_checkpoints": {
                  "fixed_1250": {"sha256": "fixed", "iteration": 1250},
                  "recent": {"sha256": "recent", "iteration": 1753}}}
    assert opponent_identity(config, "self_play", "learner") == ("learner", None)
    assert opponent_identity(config, "fixed_1250", "learner") == ("fixed", 1250)
    assert opponent_identity(config, "recent", "learner") == ("recent", 1753)
