"""Pinned checkpoint locations shared by the catalog, video and live clients."""

from pathlib import PurePosixPath
import re


SETTLEMENT_RUN = "settlement-nearquiet-formal1553-200r-20260923"
SETTLEMENT_ROOT = f"/data/firstlight/{SETTLEMENT_RUN}/training"
SETTLEMENT_CATEGORY = "settlement-nearquiet-formal1553"
SETTLEMENT_LABEL = "近静场结算 β=1/3 · 1553 起点"
FORMAL1553_SHA = "6b5143e238a30b78b93a05b605dfece6d809cc0452c4b5ef1efd0e24060d78e0"
CONTINUATION_RUN = "rl108-beta015-overflow003-20260928"
CONTINUATION_ROOT = f"/data/firstlight/{CONTINUATION_RUN}"
CONTINUATION_CATEGORY = "tower-beta015-continuation1753"
CONTINUATION_LABEL = "正式训练 · 1753 续训（塔血 β=0.15）"
CONTINUATION_START_SHA = "d576ad087a0c1efd6e3e2203ae21a717f46ed5b493e863d3794a9fec742d1c27"


def checkpoint_location(archive, entry):
    """Return the exact weight path and its allowed root, never a supplied path."""
    sha = entry.get("sha256", "")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("invalid checkpoint SHA")
    run = PurePosixPath(entry.get("run", ""))
    if run.is_absolute() or ".." in run.parts or not run.parts:
        raise ValueError("invalid checkpoint run")
    if str(run) == SETTLEMENT_RUN:
        if entry.get("category") != SETTLEMENT_CATEGORY:
            raise ValueError("settlement checkpoint category mismatch")
        root = PurePosixPath(SETTLEMENT_ROOT)
        return root / "weights" / (sha + ".pt"), root
    if str(run) == CONTINUATION_RUN:
        if entry.get("category") != CONTINUATION_CATEGORY:
            raise ValueError("continuation checkpoint category mismatch")
        root = PurePosixPath(CONTINUATION_ROOT)
        return root / "weights" / (sha + ".pt"), root
    if entry.get("category") == SETTLEMENT_CATEGORY:
        raise ValueError("settlement checkpoint run mismatch")
    if entry.get("category") == CONTINUATION_CATEGORY:
        raise ValueError("continuation checkpoint run mismatch")
    root = PurePosixPath(archive)
    return root / run / "weights" / (sha + ".pt"), root
