import importlib.util
from pathlib import Path
import struct

import pytest

spec = importlib.util.spec_from_file_location(
    "headless_compare", Path(__file__).with_name("compare_headless_snapshots.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def archive(path, rows):
    path.write_bytes(b"".join(struct.pack("<ii", tick, len(data)) + data for tick, data in rows))
    return path


def test_complete_pair_and_single_tick_difference(tmp_path):
    left = archive(tmp_path / "left", [(0, b"start"), (1, b"battle"), (2, b"end")])
    right = archive(tmp_path / "right", [(0, b"start"), (1, b"battle"), (2, b"end")])
    assert module.compare(left, right, 2)["equal"]
    archive(right, [(0, b"start"), (1, b"battlf"), (2, b"end")])
    result = module.compare(left, right, 2)
    assert result["equal"] is False
    assert result["different_ticks"] == [1]


@pytest.mark.parametrize("data", [
    b"", struct.pack("<ii", 0, 2) + b"x",
    struct.pack("<ii", 0, -1), struct.pack("<ii", 0, 0),
    struct.pack("<ii", 0, module.MAX_PAYLOAD + 1),
    struct.pack("<ii", 1, 1) + b"x",
    struct.pack("<ii", 0, 1) + b"x" + b"trailing",
])
def test_malformed_or_incomplete_archive_is_not_a_pass(tmp_path, data):
    path = tmp_path / "broken"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        module.inspect_archive(path, 0)


def test_missing_duplicate_and_extra_ticks_fail(tmp_path):
    for index, ticks in enumerate(([0, 2], [0, 0, 2], [0, 1, 2, 3])):
        path = archive(tmp_path / str(index), [(tick, b"x") for tick in ticks])
        with pytest.raises(ValueError):
            module.inspect_archive(path, 2)


def test_same_file_cannot_be_a_paired_baseline(tmp_path):
    path = archive(tmp_path / "one", [(0, b"x")])
    with pytest.raises(ValueError):
        module.compare(path, path, 0)
