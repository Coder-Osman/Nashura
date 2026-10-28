"""Bounded structural observations at version-bound catalog lookup sites."""

MASK = (1 << 56) - 1


def word(read_memory, address):
    address &= MASK
    if not address:
        raise ValueError("null lookup object")
    value = bytes(read_memory(address, 8))
    if len(value) != 8:
        raise ValueError("short lookup tree read")
    return int.from_bytes(value, "little")


def tree(read_memory, object_address):
    object_address &= MASK
    if not object_address:
        raise ValueError("null lookup object")
    return {"root_nonnull": bool(word(read_memory, object_address + 0x100) & MASK),
            "size_word": word(read_memory, object_address + 0x108)}


def found(object_address, result_address):
    object_address &= MASK
    result_address &= MASK
    if not object_address or not result_address:
        raise ValueError("null tree lookup result")
    return result_address != ((object_address + 0x100) & MASK)
