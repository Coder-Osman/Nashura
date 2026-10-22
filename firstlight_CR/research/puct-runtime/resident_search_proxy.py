"""Single-slot resident search transport for the isolated PUCT experiment."""
from __future__ import annotations

from typing import Any, Mapping


class SingleSlotResidentSearchProxy:
    def __init__(self, proxy: Any, coordinator: Any):
        if proxy.env_id != 0 or coordinator.env_ids != (0,):
            raise ValueError("resident search requires only slot 0")
        self.proxy = proxy
        self.coordinator = coordinator
        self.native = proxy.native

    def __getattr__(self, name: str) -> Any:
        return getattr(self.proxy, name)

    def _require_control(self) -> None:
        if self.coordinator.session_active:
            raise RuntimeError("close the resident batch channel before search control")
        status = self.native.resident_status()
        slots = status.get("slots", ())
        if status.get("occupied") != 1 or len(slots) != 1 or slots[0].get("envId") != 0:
            raise RuntimeError("resident search requires exactly one configured slot")

    def _request(self, command: str) -> dict[str, Any]:
        if command != "state-digest":
            return self.native._request(command)
        self._require_control()
        return self.native._raw_request("state-digest")

    def create_snapshot(self) -> dict[str, Any]:
        self._require_control()
        return self.native.create_snapshot()

    def restore(
        self, handle: int | Mapping[str, Any], *, validate_atomic: bool = True
    ) -> dict[str, Any]:
        self._require_control()
        self.proxy._cached_atomic = None
        self.proxy._cached_ordinary = None
        restored = (
            self.native.restore(handle)
            if validate_atomic
            else self.native.restore(handle, validate_atomic=False)
        )
        receipt = restored.get("restore", {})
        if restored.get("tick") != receipt.get("tick"):
            raise ValueError("resident restore receipt and observation ticks differ")
        self.proxy._cached_ordinary = restored
        self.proxy._last_stop_flags = 0
        return restored

    def release_snapshot(self, handle: int | Mapping[str, Any]) -> dict[str, Any]:
        self._require_control()
        return self.native.release_snapshot(handle)
