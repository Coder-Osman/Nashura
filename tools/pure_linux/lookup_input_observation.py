"""Read two bounded pointer-presence fields without exposing pointer values."""


MASK = (1 << 56) - 1


def observe(read_memory, object_address):
    object_address &= MASK
    if not object_address:
        raise ValueError("null lookup object")
    raw = bytes(read_memory(object_address, 8))
    if len(raw) != 8:
        raise ValueError("short lookup object read")
    first = int.from_bytes(raw, "little") & MASK
    if not first:
        return {"first_nonnull": False, "nested_nonnull": None}
    raw = bytes(read_memory(first + 8, 8))
    if len(raw) != 8:
        raise ValueError("short nested lookup object read")
    return {"first_nonnull": True, "nested_nonnull": bool(int.from_bytes(raw, "little") & MASK)}
