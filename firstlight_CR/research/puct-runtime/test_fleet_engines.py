import io
import json
import re
from types import SimpleNamespace

import pytest

import fleet_engines as engines


@pytest.mark.parametrize("workers", [1, 4])
def test_starts_only_requested_ports_and_closes_connections(tmp_path, monkeypatch, workers):
    (tmp_path / "config.json").write_text(json.dumps({"fleet": {"34": 4}}))
    monkeypatch.setattr(engines, "ROOT", tmp_path)
    monkeypatch.setattr(engines, "inventory", lambda filename: {"34": ("user", "fixture")})
    clients = []
    class Client:
        def __init__(self):
            self.closed = False
            clients.append(self)
        def exec_command(self, command, timeout):
            self.port = int(re.search(r"--port (\d+)", command).group(1))
            stdout = io.BytesIO(b'{"alive": true, "ready": true}')
            stdout.channel = SimpleNamespace(recv_exit_status=lambda: 0)
            return None, stdout, io.BytesIO()
        def close(self):
            self.closed = True
    monkeypatch.setattr(engines, "connect", lambda *args: Client())
    result = engines.start_host("34", tmp_path / "inventory", limit=2, workers=workers)
    assert result == {"host": "34", "ready": [29920, 29921], "required": 4}
    assert sorted(client.port for client in clients) == [29920, 29921]
    assert all(client.closed for client in clients)


@pytest.mark.parametrize("workers", [0, 5])
def test_invalid_concurrency_is_rejected_before_connecting(workers):
    with pytest.raises(ValueError, match="between 1 and 4"):
        engines.start_host("34", None, workers=workers)
