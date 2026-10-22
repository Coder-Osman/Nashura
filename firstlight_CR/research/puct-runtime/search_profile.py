"""Schema helpers for root search profiles."""
from __future__ import annotations

from dataclasses import dataclass, field
import resource
from typing import Any


@dataclass
class SearchProfile:
    root_id: str
    simulation_count: int
    spans: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)

    def append(self, span: str, elapsed_ms: float, owned_snapshot_count: int) -> None:
        self.spans.append({
            "span": span,
            "elapsed_ms": float(elapsed_ms),
            "owned_snapshot_count": int(owned_snapshot_count),
        })

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + int(amount)

    def set_max(self, name: str, value: int) -> None:
        self.counters[name] = max(self.counters.get(name, 0), int(value))

    def summary(self) -> dict[str, Any]:
        totals: dict[str, float] = {}
        for item in self.spans:
            name = str(item["span"])
            totals[name] = totals.get(name, 0.0) + float(item["elapsed_ms"])
        peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {
            "schema": "firstlight-search-profile.v1",
            "root_id": self.root_id,
            "simulation_count": self.simulation_count,
            "exclusive_spans_ms": totals,
            "rss_peak_mb": peak_rss_kib / 1024.0,
            "span_count": len(self.spans),
            "max_owned_snapshot_count": max(
                (item["owned_snapshot_count"] for item in self.spans), default=0
            ),
            "counters": dict(self.counters),
        }
