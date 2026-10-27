"""Read ELF64 dynamic metadata through program headers, including sectionless ELF."""
import argparse
import hashlib
import json
import struct
from pathlib import Path


class ElfError(ValueError):
    pass


class Elf64:
    def __init__(self, data):
        self.data = data
        if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
            raise ElfError("requires ELF64 little endian version 1")
        header = self.unpack("HHIQQQIHHHHHH", 16)
        self.kind, self.machine = header[:2]
        self.section_count = header[-2]
        offset, entry_size, count = header[4], header[8], header[9]
        if entry_size != 56 or count == 65535:
            raise ElfError("unsupported program header encoding")
        self.segments = []
        for index in range(count):
            values = self.unpack("IIQQQQQQ", offset + index * entry_size)
            kind, flags, start, virtual, physical, filesz, memsz, alignment = values
            self.slice(start, filesz)
            if kind == 1 and memsz < filesz:
                raise ElfError("invalid PT_LOAD size")
            self.segments.append(dict(kind=kind, flags=flags, offset=start,
                                      virtual=virtual, filesz=filesz, memsz=memsz))
        self.tags = {}
        for segment in self.segments:
            if segment["kind"] == 2:
                terminated = False
                for position in range(segment["offset"], segment["offset"] + segment["filesz"], 16):
                    tag, value = self.unpack("QQ", position)
                    if tag == 0:
                        terminated = True
                        break
                    self.tags.setdefault(tag, []).append(value)
                if not terminated:
                    raise ElfError("unterminated PT_DYNAMIC")

    def slice(self, offset, length):
        if offset < 0 or length < 0 or offset + length > len(self.data):
            raise ElfError("file range outside input")
        return self.data[offset:offset + length]

    def unpack(self, format_string, offset):
        return struct.unpack("<" + format_string, self.slice(offset, struct.calcsize("<" + format_string)))

    def address(self, virtual, length):
        for segment in self.segments:
            relative = virtual - segment["virtual"]
            if segment["kind"] == 1 and 0 <= relative and relative + length <= segment["filesz"]:
                return segment["offset"] + relative
        raise ElfError("virtual range not file-backed by PT_LOAD")

    def string(self, offset, length):
        content = self.slice(offset, length)
        end = content.find(b"\0")
        if end < 0:
            raise ElfError("unterminated string")
        return content[:end].decode("utf-8", errors="strict")

    def tag(self, number):
        values = self.tags.get(number, [])
        if len(values) != 1:
            raise ElfError("missing or duplicate dynamic tag " + str(number))
        return values[0]

    def symbol_count(self):
        if 4 in self.tags:
            return self.unpack("II", self.address(self.tag(4), 8))[1], "DT_HASH"
        if 0x6FFFFEF5 not in self.tags:
            raise ElfError("no supported dynamic symbol count source")
        base = self.tag(0x6FFFFEF5)
        bucket_count, first_symbol, bloom_count, shift = self.unpack("IIII", self.address(base, 16))
        if not bucket_count or not bloom_count:
            raise ElfError("invalid GNU hash header")
        bucket_base = base + 16 + 8 * bloom_count
        chain_base = bucket_base + 4 * bucket_count
        last = first_symbol - 1
        for index in range(bucket_count):
            symbol = self.unpack("I", self.address(bucket_base + index * 4, 4))[0]
            if symbol == 0:
                continue
            if symbol < first_symbol:
                raise ElfError("invalid GNU hash bucket")
            for unused in range(len(self.data) // 4):
                value = self.unpack("I", self.address(chain_base + (symbol - first_symbol) * 4, 4))[0]
                last = max(last, symbol)
                if value & 1:
                    break
                symbol += 1
            else:
                raise ElfError("unterminated GNU hash chain")
        return last + 1, "DT_GNU_HASH"

    def report(self):
        interpreter = None
        for segment in self.segments:
            if segment["kind"] == 3:
                interpreter = self.string(segment["offset"], segment["filesz"])
        report = {"schema": "nashura-static-elf.v1", "status": "STATIC_FOUND",
                  "sha256": hashlib.sha256(self.data).hexdigest(), "bytes": len(self.data),
                  "elf_class": 64, "endianness": "little", "machine": self.machine,
                  "type": self.kind, "section_count": self.section_count,
                  "program_header_count": len(self.segments), "interpreter": interpreter,
                  "needed": [], "undefined_symbols": [], "weak_undefined_symbols": [],
                  "defined_symbols": [], "defined_jni_exports": [],
                  "runtime_symbol_resolution": "NOT_RUN", "dynamic_symbol_count": 0,
                  "program_headers": [{key: value for key, value in segment.items()
                                       if key != "virtual"} for segment in self.segments]}
        if not self.tags:
            return report
        string_size = self.tag(10)
        string_offset = self.address(self.tag(5), string_size)

        def name_at(index):
            if index >= string_size:
                raise ElfError("string index outside DT_STRTAB")
            return self.string(string_offset + index, string_size - index)

        report["needed"] = [name_at(index) for index in self.tags.get(1, [])]
        if self.tag(11) != 24:
            raise ElfError("unsupported DT_SYMENT")
        count, source = self.symbol_count()
        table = self.address(self.tag(6), count * 24)
        report.update(dynamic_symbol_count=count, symbol_count_source=source)
        for index in range(count):
            name, info, other, section, value, size = self.unpack("IBBHQQ", table + index * 24)
            if not name:
                continue
            symbol_name = name_at(name)
            if not symbol_name:
                continue
            if section == 0:
                report["undefined_symbols"].append(symbol_name)
                if info >> 4 == 2:
                    report["weak_undefined_symbols"].append(symbol_name)
            else:
                report["defined_symbols"].append(symbol_name)
                if symbol_name == "JNI_OnLoad" or symbol_name.startswith("Java_"):
                    report["defined_jni_exports"].append(symbol_name)
        report["undefined_symbols"] = sorted(set(report["undefined_symbols"]))
        report["defined_jni_exports"] = sorted(set(report["defined_jni_exports"]))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = args.input.read_bytes()
    if hashlib.sha256(data).hexdigest() != args.sha256:
        parser.error("input identity mismatch")
    report = Elf64(data).report()
    with args.output.open("x") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ["status", "sha256", "machine", "dynamic_symbol_count"]}))


if __name__ == "__main__":
    main()
