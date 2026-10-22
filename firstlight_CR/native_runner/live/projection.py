"""Measured affine touch projection, separate from model/native orientation."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .snapshot import LiveStateError


class TouchProjection:
    def __init__(self, pairs: list[dict]):
        if len(pairs) < 4:
            raise LiveStateError("at least four measured touch/spawn pairs are required")
        positions = np.array([self.display_world(p["native"], p["owner"]) for p in pairs])
        if np.ptp(positions[:, 0]) < 12000 or np.ptp(positions[:, 1]) < 8000:
            raise LiveStateError("calibration must span both lanes and arena depth")
        design = np.column_stack([positions, np.ones(len(pairs))])
        if np.linalg.matrix_rank(design) != 3:
            raise LiveStateError("degenerate calibration")
        screens = np.array([p["screen"] for p in pairs])
        self.coefficients = np.linalg.lstsq(design, screens, rcond=None)[0]
        self.max_residual_px = float(np.linalg.norm(design @ self.coefficients - screens, axis=1).max())
        if self.max_residual_px > 40:
            raise LiveStateError("calibration residual exceeds a coarse-cell touch tolerance")

    @classmethod
    def from_files(cls, paths: list[Path]):
        return cls([json.loads(line) for path in paths for line in path.read_text().splitlines()])

    @staticmethod
    def display_world(native, owner: int):
        if owner not in (0, 1):
            raise LiveStateError("invalid camera owner")
        x, y = native
        if not 0 <= x <= 18000 or not 0 <= y <= 32000:
            raise LiveStateError("world coordinate outside arena")
        return (18000 - x, y) if owner == 0 else (x, 32000 - y)

    def screen(self, cell, owner: int, *, model_subcell_offset=None) -> tuple[int, int]:
        x, y = cell
        if type(x) is not int or type(y) is not int or not 0 <= x < 18 or not 0 <= y < 32:
            raise LiveStateError("invalid native grid cell")
        dx, dy = model_subcell_offset or (0, 0)
        if dx not in (0, 0.5) or dy not in (0, 0.5):
            raise LiveStateError("unsupported building anchor offset")
        sign = 1 if owner == 0 else -1
        position = self.display_world(((x + 0.5 + sign * dx) * 1000,
                                       (y + 0.5 + sign * dy) * 1000), owner)
        result = np.array([*position, 1]) @ self.coefficients
        pixel = tuple(int(round(v)) for v in result)
        if not 80 <= pixel[0] <= 1000 or not 150 <= pixel[1] <= 1480:
            raise LiveStateError("projected cell is outside inspected arena viewport")
        return pixel
