"""Full-width predicate and branch relocation checks; native parity separate."""
from pathlib import Path
import random
import re
from types import SimpleNamespace

import pytest

from audit_query_signed_height import audit
import run_headless_match as runner
from test_query_dimension_pair import branches, words
from test_run_headless_match import inputs, COMPLETE


def signed(value):
    return value - (1 << 32) if value & (1 << 31) else value


def test_full_width_predicate_and_dead_register_boundary():
    edges = [0, 1, 2, 31, 0x7fffffff, 0x80000000, 0xffffffff]
    rng = random.Random(20261031)
    pairs = [(h, y) for h in edges for y in edges]
    pairs.extend((rng.getrandbits(32), rng.getrandbits(32)) for _ in range(100000))
    for height, y in pairs:
        accepted = signed(y) >= 0 and signed(height) > signed(y)
        assert accepted == (signed(height) > y)
        if accepted:
            assert height == signed(height)


def test_relocated_branches_preserve_exits_and_replace_only_sign_test():
    header = Path(__file__).with_name("headless_query_signed_height.h").read_text()
    block = header.split("query_signed_height_replacement[] = {", 1)[1].split("};", 1)[0]
    new = [int(x, 16) for x in re.findall(r"0x[0-9a-f]+", block)]
    old = words("query_dimension_original")
    assert len(new) == len(old) == 19
    assert new[:3] == old[:3]
    assert new[3:6] == [0xb98006e9, 0xeb3b413f, 0x54ffff0d]
    assert branches(old)[:1] + branches(old)[2:] == branches(new)
    assert new[6:18] == words("query_dimension_replacement")[6:18]
    assert "engine+0xf34c60),reject,sizeof(reject)" in header
    assert "PROT_READ|PROT_WRITE|PROT_EXEC" not in header


@pytest.mark.parametrize("index", range(7))
def test_all_ordered_combination_markers_required(inputs, monkeypatch, index):
    settings, output = inputs
    markers = runner.optimization_installation_markers("hot-distance-component-height-v1")
    assert len(markers) == 7
    markers.pop(index)
    def execute(command, *, env, stdout, stderr):
        stdout.write((chr(10).join(markers) + chr(10) + COMPLETE).encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, "run", execute)
    with pytest.raises(ValueError, match="installation evidence"):
        runner.run(settings, output, apply=True, snapshot_trace=False,
            native_optimization="hot-distance-component-height-v1")


def test_static_audit_rejects_wrong_identity(tmp_path):
    path = tmp_path / "wrong.so"
    path.write_bytes(b"synthetic wrong engine")
    with pytest.raises(ValueError, match="exact pinned"):
        audit(path)
