import struct

import pytest

from catalog_table_counts import SLOTS, snapshot


def test_snapshot_checks_owner_type_and_count():
    root = bytearray(219 * 8)
    header = bytearray(0x50)
    struct.pack_into("<Q", root, SLOTS[0] * 8, 0x2000)
    struct.pack_into("<ii", header, 0, 8, 3)
    struct.pack_into("<Q", header, 0x40, 0x1000)
    struct.pack_into("<i", header, 0x4c, SLOTS[0])

    def read(address, size):
        if (address, size) == (0x1000, len(root)):
            return root
        if (address, size) == (0x2038, len(header)):
            return header
        raise ValueError("unexpected read")

    result = snapshot(read, 0xab00000000001000)
    assert result[0] == [SLOTS[0], 3]
    assert all(count == -1 for _, count in result[1:])
    struct.pack_into("<i", header, 0x4c, 0)
    with pytest.raises(ValueError, match="invariant"):
        snapshot(read, 0x1000)
