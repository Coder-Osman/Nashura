import pytest

from lookup_input_observation import observe


def test_reads_presence_and_handles_tagged_pointers():
    values = {0x1000: 0xab00000000002000, 0x2008: 0xcd00000000003000}
    assert observe(lambda address, size: values[address].to_bytes(size, "little"), 0xef00000000001000) == {
        "first_nonnull": True, "nested_nonnull": True}
    values[0x1000] = 0
    assert observe(lambda address, size: values[address].to_bytes(size, "little"), 0x1000) == {
        "first_nonnull": False, "nested_nonnull": None}


def test_short_read_and_null_fail_explicitly():
    with pytest.raises(ValueError, match="null"):
        observe(lambda address, size: b"", 0)
    with pytest.raises(ValueError, match="short"):
        observe(lambda address, size: b"", 0x1000)
