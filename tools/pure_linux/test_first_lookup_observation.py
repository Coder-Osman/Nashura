import pytest

from first_lookup_observation import found, tree


def memory(address, size):
    values = {0x1100: 0xabc, 0x1108: 3}
    return values[address].to_bytes(size, "little")


def test_tree_and_sentinel_are_observed_without_pointer_values():
    assert tree(memory, 0x1000) == {"root_nonnull": True, "size_word": 3}
    assert found(0x1000, 0x1100) is False
    assert found(0x1000, 0x1200) is True


def test_tagged_addresses_and_missing_reads():
    assert found(0xab00000000001000, 0xcd00000000001100) is False
    with pytest.raises(ValueError):
        found(0x1000, 0)
    with pytest.raises(ValueError):
        tree(lambda address, size: b"", 0x1000)
