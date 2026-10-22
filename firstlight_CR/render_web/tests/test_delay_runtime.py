import hashlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from render_web import remote_agent
from render_web.config import write_json
from render_web.generate_match import validate_rollout
from render_web.runtime_constraints import ACTION_SPACE, delay_constraints_contract
from render_web.tests.test_workbench import api


@pytest.fixture
def frozen(tmp_path):
    source = tmp_path / "archive/run/collector-scheduler-code"
    (source / ACTION_SPACE).parent.mkdir(parents=True)
    code = (Path(__file__).parents[2] / ACTION_SPACE).read_bytes()
    (source / ACTION_SPACE).write_bytes(code)
    scheduler = "native_runner/training/event_scheduling.py"
    (source / scheduler).write_text('EVENT_SCHEDULER_VERSION = "event-scheduling.v2"\n')
    manifest = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                for name in (ACTION_SPACE, scheduler)}
    write_json(source.parent / "collector-scheduler-manifest.json", manifest)
    cfg = dict(archive_root=str(tmp_path / "archive"), source_run="run", remote_root=str(tmp_path / "remote"))
    return source, cfg, manifest


def test_new_video_requests_record_delay_constraint_and_legacy_retry_preserves_identity(api):
    client, store, _ = api
    new = client.post("/api/jobs", json=dict(top="top")).json()
    assert new["request"]["action_constraints"] == delay_constraints_contract()
    old_request = dict(store.get(new["id"])["request"])
    old_request.pop("action_constraints")
    old = store.create(old_request)
    store.cancel(old["id"])
    retry = client.post(f"/api/jobs/{old['id']}/retry").json()
    assert "action_constraints" not in retry["request"]


def test_new_runtime_has_separate_verified_identity_and_preserves_original(frozen):
    source, cfg, original = frozen
    (source / ".venv").symlink_to("/fixture/shared-venv", target_is_directory=True)
    old, old_manifest = remote_agent.runtime(cfg)
    new, manifest = remote_agent.runtime(cfg, delay_constraints_contract())
    assert old != new and old_manifest == original
    assert (new / ".venv").is_symlink()
    assert remote_agent.runtime(cfg)[0] == old
    assert remote_agent.runtime(cfg, delay_constraints_contract())[0] == new
    for name, digest in original.items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest
        assert hashlib.sha256((old / name).read_bytes()).hexdigest() == digest
    for name, digest in manifest.items():
        assert hashlib.sha256((new / name).read_bytes()).hexdigest() == digest
    assert manifest["native_runner/training/event_scheduling.py"] == original["native_runner/training/event_scheduling.py"]
    identity = json.loads((new / "runtime-identity.json").read_text())
    assert identity["action_constraints"] == delay_constraints_contract()
    assert identity["manifest"] == original
    (new / ACTION_SPACE).write_text("tampered")
    with pytest.raises(ValueError, match="manifest mismatch"):
        remote_agent.runtime(cfg, delay_constraints_contract())


def test_source_validation_and_unsupported_contract_fail_closed(frozen):
    source, cfg, _ = frozen
    with pytest.raises(ValueError, match="unsupported"):
        remote_agent.runtime(cfg, {"delay": {"minimum_ms": 0}})
    (source / ACTION_SPACE).write_text("tampered")
    with pytest.raises(ValueError, match="source runtime manifest mismatch"):
        remote_agent.runtime(cfg, delay_constraints_contract())


def test_constraint_implementation_changes_runtime_identity(frozen, monkeypatch):
    _, cfg, _ = frozen
    first, _ = remote_agent.runtime(cfg, delay_constraints_contract())
    original = remote_agent.runtime_overlay
    def changed(source):
        result = original(source)
        result[ACTION_SPACE] += b"\n# modified implementation\n"
        return result
    monkeypatch.setattr(remote_agent, "runtime_overlay", changed)
    second, _ = remote_agent.runtime(cfg, delay_constraints_contract())
    assert first != second


@pytest.mark.parametrize("tick,allowed", [(0, True), (5997, True), (5998, False)])
def test_backported_runtime_filters_act_and_card_before_sampling(frozen, monkeypatch, tick, allowed):
    pytest.importorskip("torch")
    from native_runner.tests.test_event_action_space import _catalog, _deck, _environment, _frame, _grid, _observation, _placement
    _, cfg, _ = frozen
    root, _ = remote_agent.runtime(cfg, delay_constraints_contract())
    name = "native_runner.training.test_frozen_delay"
    module = ModuleType(name)
    module.__package__ = "native_runner.training"
    monkeypatch.setitem(sys.modules, name, module)
    exec(compile((root / ACTION_SPACE).read_text(), str(root / ACTION_SPACE), "exec"), module.__dict__)
    catalog, deck = _catalog(), _deck()
    environment = _environment({"0": _placement(_grid([(1, 1)]))})
    observation = _observation([deck[0]])
    observation.tick = tick
    legal = module.legal_event_actions(environment, observation, _frame(catalog, deck), 0,
        catalog=catalog, card_count=8, cell_count=576, board_width=18, board_height=32,
        delay_offset_ms=(0, 50, 100, 6000))
    assert legal.act_allowed == allowed
    assert not legal.delay_masks[:, :2].any()
    assert bool(legal.card_mask.any()) == allowed
    assert bool(legal.cell_masks.any()) == allowed


def test_video_validation_rejects_short_delay_actions():
    counters = dict(terminal=True, scheduler_version="event-scheduling.v2",
                    not_ready=0, cancelled_window=0, act_violation=0, executed=1)
    terminal = dict(reason="native_final_result")
    row = SimpleNamespace(act=True, delay_bin=0, done=True, truncated=False)
    validate_rollout(counters, terminal, {0: [row]}, 1, False)
    with pytest.raises(ValueError, match="最短延迟"):
        validate_rollout(counters, terminal, {0: [row]}, 1, False, minimum_delay_ms=100, delay_offset_ms=(0, 100))
    row.delay_bin = 1
    validate_rollout(counters, terminal, {0: [row]}, 1, False, minimum_delay_ms=100, delay_offset_ms=(0, 100))
