import json
from pathlib import Path
import time
from types import SimpleNamespace

import train_with_teacher as entry


def complete_inputs():
    config = json.loads((Path(__file__).parent / "config.json").read_text())
    fleet = dict(checked_at=time.time(), passed=True, blockers=[], hosts={host: dict(
        disk_free_gib=100, memory_available_gib=64, ready_task_engines=count)
        for host, count in config["fleet"].items()})
    teachers = [SimpleNamespace(episode_id=f"game-{i}", search_simulations=128) for i in range(216)]
    return config, dict(teachers=teachers, summaries=list(range(108)), fleet_report=fleet,
                        prototype=dict(passed=True, games=108, simulations=32))


def test_single_root_acceptance_does_not_unlock_formal_training():
    config, inputs = complete_inputs()
    assert entry.formal_blockers(config, **inputs) == []
    inputs["prototype"] = dict(passed=True, simulations=32, games=1)
    assert any("108 complete games" in item for item in entry.formal_blockers(config, **inputs))


def test_incomplete_fleet_or_low_disk_blocks_formal_start():
    config, inputs = complete_inputs()
    inputs["fleet_report"]["hosts"]["34"]["disk_free_gib"] = 9.6
    inputs["fleet_report"]["hosts"]["37"]["ready_task_engines"] = 2
    errors = entry.formal_blockers(config, **inputs)
    assert any("9.6 GiB" in item for item in errors)
    assert any("2/24" in item for item in errors)


def test_failed_or_unverified_native_preflight_blocks_formal_start():
    config, inputs = complete_inputs()
    for passed in (False, None):
        inputs["fleet_report"]["passed"] = passed
        assert any("runtime hash preflight" in error for error in entry.formal_blockers(config, **inputs))
    inputs["fleet_report"].update(passed=True, blockers=["runtime hash mismatch"])
    assert any("runtime hash preflight" in error for error in entry.formal_blockers(config, **inputs))


def test_formal_start_checks_gates_before_loading_model_or_optimizer(tmp_path, monkeypatch):
    source = Path(__file__).parent
    for name in ("config.json", "match-plan.json"):
        (tmp_path / name).write_text((source / name).read_text())
    monkeypatch.setattr(entry, "ROOT", tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("model/optimizer loading must follow formal gates")
    monkeypatch.setattr(entry, "load_event_checkpoint", forbidden)
    monkeypatch.setattr(entry, "restore_optimizer", forbidden)
    result = entry.update_once(checkpoint=tmp_path / "missing.pt", teacher_paths=[],
                               rollout_root=tmp_path / "absent", output=tmp_path / "child.pt", mode="formal")
    assert result["status"] == "blocked_before_training" and result["optimizer_steps"] == 0
    assert not (tmp_path / "child.pt").exists()
