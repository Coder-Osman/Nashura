from pathlib import Path
import fcntl
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from render_web.app import create_app
from render_web.config import Config
from render_web.human import (
    HumanSession,
    HumanSessionManager,
    _observe_with_retry,
    _publish_observation,
    _rebind_ai_card_action,
)
from native_runner.contracts import ActionKind, ActionV1
from native_runner.cr_native_env import RunnerError


def test_human_play_markup_matches_client_state_hooks():
    html = (Path(__file__).parents[1] / "static" / "play.html").read_text()
    assert 'id="screen-wrap"' in html


def test_human_entry_opens_native_friendly_mode(tmp_path):
    app = create_app(Config(data=tmp_path), start_worker=False)
    with TestClient(app) as client:
        response = client.get("/play", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/live/?mode=friendly"
        page = client.get("/play")
        assert page.status_code == 200
        assert 'id="match-mode"' in page.text
        assert "HumanPilot" in page.text
        assert "firstlight-human" in page.text


def checkpoint():
    return {
        "id": "human-checkpoint",
        "sha256": "a" * 64,
        "label": "RL 171",
        "iteration": 171,
        "version": "event-checkpoint.v3",
        "supported": True,
        "available": True,
        "compatibility": "same",
        "path": "/weights/model.pt",
        "source": "local",
    }


class Catalog:
    def __init__(self, row):
        self.row = row

    def start(self):
        pass

    def close(self):
        pass

    def get(self, identity):
        if identity != self.row["id"]:
            raise ValueError("missing checkpoint")
        return dict(self.row)

    def items(self):
        return [dict(self.row)]


def test_human_waits_for_video_worker_lock(tmp_path, monkeypatch):
    manager = HumanSessionManager(Config(data=tmp_path), None)
    session = HumanSession("test", checkpoint(), 1, manager.data / "test")
    entered = threading.Event()
    monkeypatch.setattr(manager, "_run_owned", lambda _: entered.set())
    lock_path = tmp_path / "worker.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        worker = threading.Thread(target=manager._run, args=(session,))
        worker.start()
        assert not entered.wait(.2)
    assert entered.wait(2)
    worker.join(2)
    assert not worker.is_alive()


def test_stop_preserves_completed_result(tmp_path):
    manager = HumanSessionManager(Config(data=tmp_path), None)
    session = HumanSession("test", checkpoint(), 1, manager.data / "test", state="ended", winner=0)
    manager.sessions[session.identity] = session
    assert manager.stop_session(session.identity).state == "ended"
    assert not session.stop.is_set()


def test_human_api_keeps_ai_top_and_maps_bottom_click(tmp_path, monkeypatch):
    config = Config(data=Path(tmp_path), min_free_bytes=0)
    app = create_app(config, start_worker=False)
    catalog = Catalog(checkpoint())
    app.state.live_service.catalog = catalog
    manager = app.state.human_manager
    monkeypatch.setattr(manager, "_run", lambda session: None)
    with TestClient(app) as client:
        response = client.post("/api/human/sessions", json={"checkpoint": "human-checkpoint", "seed": 7})
        assert response.status_code == 202
        created = response.json()
        assert created["owner_top"] == "AI"
        assert created["owner_bottom"] == "人类"
        identity = created["id"]

        session = manager.get(identity)
        session.update(state="ready", phase="对局进行中")
        queued = client.post(
            f"/api/human/sessions/{identity}/play",
            json={"hand_index": 2, "source_x": 500, "source_y": 1200},
        )
        assert queued.status_code == 200
        assert session.pending_human == {"hand_index": 2, "world_x": 8500, "world_y": 19500}

        assert client.post(f"/api/human/sessions/{identity}/stop").status_code == 200


def test_human_screen_is_png_and_uncached(tmp_path, monkeypatch):
    config = Config(data=Path(tmp_path), min_free_bytes=0)
    app = create_app(config, start_worker=False)
    catalog = Catalog(checkpoint())
    app.state.live_service.catalog = catalog
    manager = app.state.human_manager
    monkeypatch.setattr(manager, "_run", lambda session: None)
    with TestClient(app) as client:
        identity = client.post("/api/human/sessions", json={"checkpoint": "human-checkpoint"}).json()["id"]
        monkeypatch.setattr(manager, "screen", lambda value: b"\x89PNG\r\nfixture")
        response = client.get(f"/api/human/sessions/{identity}/screen")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
        assert response.headers["cache-control"] == "no-store"
        assert response.content.startswith(b"\x89PNG")


def test_ai_action_rebinds_current_native_hand_slot():
    environment = SimpleNamespace(
        raw_observation={
            "players": [{
                "owner": 0,
                "hand": [{"handIndex": 3, "cardId": 26000001}],
            }],
        }
    )
    action = ActionV1.play(owner=0, hand_slot=0, card_id=26000001, grid=(2, 7))

    rebound = _rebind_ai_card_action(environment, action)

    assert rebound.hand_slot == 3
    assert rebound.card_id == 26000001
    assert rebound.metadata["hand_slot_rebound"] is True


def test_ai_action_rebind_rejects_card_that_left_hand():
    environment = SimpleNamespace(
        raw_observation={"players": [{"owner": 0, "hand": []}]}
    )
    action = ActionV1.play(owner=0, hand_slot=0, card_id=26000001, grid=(2, 7))

    with pytest.raises(RunnerError, match="no longer in the native hand"):
        _rebind_ai_card_action(environment, action)


def _native_frame(tick=90):
    return {
        "tick": tick,
        "players": [
            {"owner": 0, "elixir": 5.0, "hand": []},
            {
                "owner": 1,
                "elixir": 4.5,
                "hand": [
                    {"handIndex": index, "cardId": 26000000 + index, "cost": 2 + index}
                    for index in range(4)
                ],
            },
        ],
    }


def test_first_native_observation_publishes_human_hand(tmp_path):
    session = HumanSession("test", checkpoint(), 1, tmp_path / "session")

    view = _publish_observation(session, _native_frame(), 0)

    assert session.tick == 90
    assert view["bottom"]["elixir"] == 4.5
    assert len(view["bottom"]["hand"]) == 4
    assert [card["hand_index"] for card in view["bottom"]["hand"]] == [0, 1, 2, 3]
    assert session.last_state == view


def test_transient_native_observation_failure_retries():
    class Native:
        def __init__(self):
            self.calls = 0

        def observe(self):
            self.calls += 1
            if self.calls == 1:
                raise RunnerError("native runner transport failed for 'observe'")
            return _native_frame(91)

    frame, error = _observe_with_retry(Native())

    assert error is None
    assert frame["tick"] == 91


def test_repeated_transient_observation_failure_returns_error_for_cached_state():
    class Native:
        def observe(self):
            raise RunnerError("persistent native control session ended mid-response")

    frame, error = _observe_with_retry(Native())

    assert frame is None
    assert error is not None
    assert "persistent native control session ended" in str(error)


def test_ai_waits_before_policy_decision_tick_90(tmp_path):
    manager = HumanSessionManager(Config(data=tmp_path), None)
    action = manager._event_ai_action(
        None, None, SimpleNamespace(tick=89), None, None, None, None
    )

    assert action.kind == ActionKind.WAIT
    assert action.owner == 0
