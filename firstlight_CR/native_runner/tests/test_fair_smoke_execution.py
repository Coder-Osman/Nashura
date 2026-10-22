import json
from types import SimpleNamespace

import pytest
import torch

from native_runner.training.fair_search import smoke


@pytest.mark.parametrize("external,legacy,expected_device", [
    (True, False, "cpu"), (True, True, "cuda:0"),
    (False, False, "cuda:0"), (False, True, "cuda:0"),
])
def test_smoke_uses_cpu_actor_with_remote_inference_and_direct_submission(
        tmp_path, monkeypatch, capsys, external, legacy, expected_device):
    loaded, runtimes, clients = [], [], []
    model = SimpleNamespace(eval=lambda: None, policy=object(), value_head_architecture="test",
                            parameters=lambda: iter([torch.empty(1)]))

    def load(*args, **kwargs):
        loaded.append(kwargs)
        return model, "a" * 64

    def runtime(*args, **kwargs):
        runtimes.append(kwargs)
        return SimpleNamespace()

    def remote(*args):
        client = SimpleNamespace(closed=False)
        client.close = lambda: setattr(client, "closed", True)
        clients.append(client)
        return client

    def collect(match, beliefs, inference, **kwargs):
        assert len(beliefs) == len(inference) == 2
        assert kwargs["current_sha256"] == "a" * 64
        return {"status": "tested"}

    monkeypatch.setattr(smoke, "load_selfplay_checkpoint", load)
    monkeypatch.setattr(smoke, "install_puct", lambda path: object())
    monkeypatch.setattr(smoke, "_sha256", lambda path: "b" * 64)
    monkeypatch.setattr(smoke.importlib, "import_module", lambda name: SimpleNamespace(__file__=str(tmp_path)))
    monkeypatch.setattr(smoke, "NativeMatchRuntime", runtime)
    monkeypatch.setattr(smoke, "NativeReplayFactory", lambda *args, **kwargs: object())
    monkeypatch.setattr(smoke, "IncrementalBeliefProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(smoke, "SocketInferenceClient", remote)
    monkeypatch.setattr(smoke, "LocalInference", lambda *args, **kwargs: object())
    monkeypatch.setattr(smoke, "collect_episode", collect)
    arguments = ["--checkpoint", str(tmp_path / "model.pt"), "--puct-root", str(tmp_path),
                 "--main-port", "1234", "--belief-port", "1235", "--opponent-belief-port", "1236",
                 "--output", str(tmp_path / "output"), "--device", "cuda:0"]
    if external:
        arguments.extend(["--inference-socket", str(tmp_path / "inference.sock")])
    if legacy:
        arguments.append("--record-policy-steps")
    assert smoke.main(arguments) == 0
    assert loaded[0]["device"] == expected_device
    assert runtimes[0]["execution_only"] == (not legacy)
    assert all(client.closed for client in clients)
    assert len(clients) == (2 if external else 0)
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "tested"
    assert result["checkpoint_sha256"] == "a" * 64
