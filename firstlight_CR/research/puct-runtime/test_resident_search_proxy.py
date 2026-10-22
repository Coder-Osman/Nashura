from types import SimpleNamespace

import pytest

from resident_search_proxy import SingleSlotResidentSearchProxy


class FakeNative:
    def __init__(self):
        self.calls = []
        self.tick = 7
        self.slots = [{"envId": 0}]

    def resident_status(self):
        self.calls.append("status")
        return {"occupied": len(self.slots), "slots": self.slots}

    def observe(self):
        self.calls.append("observe")
        return {"tick": self.tick}

    def _raw_request(self, command):
        self.calls.append(command)
        return {"tick": self.tick, "sha256": "a" * 64}

    def create_snapshot(self):
        self.calls.append("create")
        return {"handle": 3, "tick": self.tick}

    def restore(self, handle):
        self.calls.append(("restore", handle))
        return {"tick": self.tick, "restore": {"tick": self.tick}}

    def release_snapshot(self, handle):
        self.calls.append(("release", handle))
        return {"released": True}


def make_proxy():
    native = FakeNative()
    proxy = SimpleNamespace(env_id=0, native=native, _cached_atomic=object(),
                            _cached_ordinary={"tick": 8}, _last_stop_flags=7)
    coordinator = SimpleNamespace(env_ids=(0,), session_active=False)
    return SingleSlotResidentSearchProxy(proxy, coordinator), native, proxy, coordinator


def test_resident_search_requires_closed_single_slot():
    search, native, _, coordinator = make_proxy()
    coordinator.session_active = True
    with pytest.raises(RuntimeError, match="close"):
        search.create_snapshot()
    coordinator.session_active = False
    native.slots.append({"envId": 1})
    with pytest.raises(RuntimeError, match="exactly one"):
        search.create_snapshot()
    assert "create" not in native.calls


def test_resident_digest_binds_slot_and_checks_tick():
    search, native, _, _ = make_proxy()
    assert search._request("state-digest")["sha256"] == "a" * 64
    assert native.calls[-2:] == ["observe", "state-digest"]
    native._raw_request = lambda _: {"tick": 8, "sha256": "b" * 64}
    with pytest.raises(ValueError, match="ticks differ"):
        search._request("state-digest")


def test_resident_restore_invalidates_atomic_capture():
    search, native, proxy, _ = make_proxy()
    assert search.create_snapshot()["handle"] == 3
    assert search.restore(3)["tick"] == 7
    assert proxy._cached_atomic is None
    assert proxy._cached_ordinary["tick"] == 7
    assert proxy._last_stop_flags == 0
    assert search.release_snapshot(3)["released"]
    assert ("restore", 3) in native.calls
