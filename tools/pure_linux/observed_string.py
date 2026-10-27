"""Bounded reads of version-identified native strings for private GDB evidence."""


def read_string(read_memory, address, encoding, limit=4096):
    if encoding not in {"engine-string16", "libcxx-string24"}:
        raise ValueError("unsupported string encoding")
    if not isinstance(limit, int) or not 0 <= limit <= 65536:
        raise ValueError("invalid observation limit")
    address &= 0x00ffffffffffffff
    if not address:
        raise ValueError("null string object")
    size = 16 if encoding == "engine-string16" else 24
    header = bytes(read_memory(address, size))
    if len(header) != size:
        raise ValueError("short object read")
    if encoding == "engine-string16":
        length = int.from_bytes(header[4:8], "little", signed=True)
        inline = length < 8
        pointer = address + 8 if inline else int.from_bytes(header[8:16], "little")
    else:
        inline = not header[0] & 1
        length = header[0] >> 1 if inline else int.from_bytes(header[8:16], "little")
        pointer = address + 1 if inline else int.from_bytes(header[16:24], "little")
        if inline and length > 22:
            raise ValueError("invalid inline string length")
    pointer &= 0x00ffffffffffffff
    if length < 0 or length > limit or not pointer:
        raise ValueError("string outside observation bounds")
    data = bytes(read_memory(pointer, length + 1))
    if len(data) != length + 1 or data[-1] != 0:
        raise ValueError("missing string terminator")
    return {"encoding": encoding, "length": length, "inline": inline,
            "bytes_hex": data[:-1].hex(), "terminated": True}
