"""Read validated, bounded catalog slot counts from a paused native process."""

import struct


SLOTS = (9, 10, 22, 34, 35, 123, 133, 166, 173, 179, 187, 189, 191, 205, 212, 213, 214)
POINTER_MASK = (1 << 56) - 1


def snapshot(read_memory, context):
    context &= POINTER_MASK
    if not context:
        raise ValueError("null catalog context")
    root = bytes(read_memory(context, 219 * 8))
    if len(root) != 219 * 8:
        raise ValueError("short catalog context")
    result = []
    for slot in SLOTS:
        address = struct.unpack_from("<Q", root, slot * 8)[0] & POINTER_MASK
        if not address:
            result.append([slot, -1])
            continue
        header = bytes(read_memory(address + 0x38, 0x50))
        if len(header) != 0x50:
            raise ValueError("short table header")
        capacity, count = struct.unpack_from("<ii", header)
        owner = struct.unpack_from("<Q", header, 0x40)[0] & POINTER_MASK
        kind = struct.unpack_from("<i", header, 0x4c)[0]
        if owner != context or kind != slot or not 0 <= count <= capacity <= 1000000:
            raise ValueError("catalog table invariant failed")
        result.append([slot, count])
    return result
