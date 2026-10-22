import json
from types import SimpleNamespace

import pytest

from native_runner.training.fair_search import value_evaluate


def test_direct_shard_two_engines_two_sides_and_resume(tmp_path, monkeypatch):
    training = tmp_path / "training"
    training.mkdir()
    (training / "done.json").write_text(json.dumps(dict(acceptance=dict(passed=True), candidate_sha256="a" * 64)))
    model = SimpleNamespace()
    model.eval = lambda: model
    model.requires_grad_ = lambda value: model
    monkeypatch.setattr(value_evaluate, "load_checkpoint", lambda *args, **kwargs: (model, {}, "a" * 64))
    monkeypatch.setattr(value_evaluate, "policy_digest", lambda model: "b" * 64)
    monkeypatch.setattr(value_evaluate, "install_puct", lambda project: object())
    monkeypatch.setattr(value_evaluate.signal, "signal", lambda *args: None)
    monkeypatch.setattr(value_evaluate, "wait_for_engine", lambda *args: None)
    engines, games = [], []

    def launch(command, **kwargs):
        engines.append(command)
        return SimpleNamespace(terminate=lambda: None, wait=lambda **kwargs: 0)

    def play(*args, group, pair, owner, progress):
        games.append((group, pair, owner))
        return dict(group=group, pair=pair, candidate_owner=owner, score=float(owner == 0))

    monkeypatch.setattr(value_evaluate.subprocess, "Popen", launch)
    monkeypatch.setattr(value_evaluate, "play_game", play)
    args = SimpleNamespace(root=tmp_path, slot=63, device="cpu", output=tmp_path / "shard",
                           group="direct", start_pair=63, pairs=1, port=31000)
    value_evaluate.worker(args)
    assert games == [("direct", 63, 0), ("direct", 63, 1)]
    assert [command[command.index("--port") + 1] for command in engines] == ["31000", "31001"]
    rows = [json.loads(line) for line in (args.output / "matches.jsonl").read_text().splitlines()]
    assert all(row["candidate_sha256"] == "a" * 64 for row in rows)
    assert json.loads((args.output / "done.json").read_text())["games"] == 2
    value_evaluate.worker(args)
    assert len(games) == 2
    args.start_pair = 64
    with pytest.raises(ValueError, match="range"):
        value_evaluate.worker(args)
    args.start_pair = 63
    args.candidate_owner = 1
    args.output = tmp_path / "single-owner"
    value_evaluate.worker(args)
    assert games[-1] == ("direct", 63, 1) and len(games) == 3
    assert json.loads((args.output / "done.json").read_text())["games"] == 1
