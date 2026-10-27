import pytest

from observed_string import read_string


def memory_image(encoding, content, inline, tagged=False):
    memory = bytearray(8192)
    address, pointer = 128, 512
    length = len(content)
    if encoding == "engine-string16":
        memory[address + 4:address + 8] = length.to_bytes(4, "little", signed=True)
        if inline:
            pointer = address + 8
        else:
            memory[address + 8:address + 16] = (pointer | ((0xab << 56) if tagged else 0)).to_bytes(8, "little")
    elif inline:
        memory[address] = length << 1
        pointer = address + 1
    else:
        memory[address] = 1
        memory[address + 8:address + 16] = length.to_bytes(8, "little")
        memory[address + 16:address + 24] = pointer.to_bytes(8, "little")
    memory[pointer:pointer + length + 1] = content + b"\0"
    return memory, address, pointer


@pytest.mark.parametrize("encoding,length,inline", [
    ("engine-string16", 0, True), ("engine-string16", 7, True),
    ("engine-string16", 8, False), ("engine-string16", 4095, False),
    ("libcxx-string24", 0, True), ("libcxx-string24", 22, True),
    ("libcxx-string24", 23, False), ("libcxx-string24", 4095, False),
])
def test_read_strings(encoding, length, inline):
    content = b"a" * length
    memory, address, pointer = memory_image(encoding, content, inline, tagged=True)
    observed = read_string(lambda start, count: memory[start:start + count], address, encoding)
    assert bytes.fromhex(observed["bytes_hex"]) == content
    assert observed["length"] == length and observed["inline"] == inline


def test_bounds_before_payload_read():
    memory, address, pointer = memory_image("engine-string16", b"12345678", False)
    calls = []

    def read(start, count):
        calls.append((start, count))
        return memory[start:start + count]

    with pytest.raises(ValueError, match="bounds"):
        read_string(read, address, "engine-string16", 7)
    assert calls == [(address, 16)]
    memory[address + 4:address + 8] = (-1).to_bytes(4, "little", signed=True)
    with pytest.raises(ValueError, match="bounds"):
        read_string(read, address, "engine-string16")


def test_reject_invalid_object_and_terminator():
    memory, address, pointer = memory_image("libcxx-string24", b"a", True)
    read = lambda start, count: memory[start:start + count]
    with pytest.raises(ValueError, match="null"):
        read_string(read, 0, "libcxx-string24")
    memory[pointer + 1] = 1
    with pytest.raises(ValueError, match="terminator"):
        read_string(read, address, "libcxx-string24")
    memory[address] = 46
    with pytest.raises(ValueError, match="inline"):
        read_string(read, address, "libcxx-string24")
    with pytest.raises(ValueError, match="encoding"):
        read_string(read, address, "unknown")
    with pytest.raises(ValueError, match="limit"):
        read_string(read, address, "engine-string16", 100000)
