"""Inspect versioned symbol candidates and loader metadata without loading ELF."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from audit_inputs import audit, member
from elf_audit import Elf64, ElfError


RELOCATION_TYPES = {
    0: "NONE", 257: "ABS64", 1024: "COPY", 1025: "GLOB_DAT",
    1026: "JUMP_SLOT", 1027: "RELATIVE", 1028: "TLS_DTPMOD64",
    1029: "TLS_DTPREL64", 1030: "TLS_TPREL64", 1031: "TLSDESC", 1032: "IRELATIVE",
}
RELOCATION_WIDTHS = {257: 8, 1025: 8, 1026: 8, 1027: 8, 1028: 8,
                     1029: 8, 1030: 8, 1031: 16, 1032: 8}


class LoaderMetadata:
    def __init__(self, elf):
        if elf.machine != 183:
            raise ElfError("loader audit requires AArch64 ELF")
        self.elf = elf
        self.string_size = elf.tag(10)
        self.string_offset = elf.address(elf.tag(5), self.string_size)
        self.requirements = {}
        self.definitions = {}
        self.read_versions()

    def name(self, index):
        if index >= self.string_size:
            raise ElfError("version/symbol string outside DT_STRTAB")
        return self.elf.string(self.string_offset + index, self.string_size - index)

    def read_at(self, layout, address, size):
        return self.elf.unpack(layout, self.elf.address(address, size))

    def chain(self, base, count, layout, size, next_index):
        if not count or count > len(self.elf.data) // size:
            raise ElfError("invalid version chain count")
        position = base
        for index in range(count):
            row = self.read_at(layout, position, size)
            yield position, row
            following = row[next_index]
            if index == count - 1:
                if following:
                    raise ElfError("version chain longer than declared")
            elif following < size:
                raise ElfError("version chain ends early or overlaps")
            position += following

    def read_versions(self):
        elf = self.elf
        for address_tag, count_tag in [(0x6FFFFFFE, 0x6FFFFFFF), (0x6FFFFFFC, 0x6FFFFFFD)]:
            if (address_tag in elf.tags) != (count_tag in elf.tags):
                raise ElfError("incomplete version table tags")
        if 0x6FFFFFFE in elf.tags:
            for position, row in self.chain(elf.tag(0x6FFFFFFE), elf.tag(0x6FFFFFFF), "HHIII", 16, 4):
                version, count, filename, auxiliary, following = row
                if version != 1 or auxiliary < 16:
                    raise ElfError("invalid VERNEED header")
                for unused, item in self.chain(position + auxiliary, count, "IHHII", 16, 4):
                    hash_value, flags, index, name, next_aux = item
                    self.add_version(self.requirements, index & 0x7FFF, {
                        "name": self.name(name), "hash": hash_value,
                        "file": self.name(filename), "flags": flags})
        if 0x6FFFFFFC in elf.tags:
            for position, row in self.chain(elf.tag(0x6FFFFFFC), elf.tag(0x6FFFFFFD), "HHHHIII", 20, 6):
                version, flags, index, count, hash_value, auxiliary, following = row
                if version != 1 or auxiliary < 20:
                    raise ElfError("invalid VERDEF header")
                names = [self.name(item[0]) for unused, item in
                         self.chain(position + auxiliary, count, "II", 8, 1)]
                self.add_version(self.definitions, index & 0x7FFF, {
                    "name": names[0], "hash": hash_value, "parents": names[1:], "flags": flags})

    @staticmethod
    def add_version(destination, index, row):
        if index in destination or index == 0:
            raise ElfError("duplicate or local version definition")
        destination[index] = row

    def symbols(self):
        elf = self.elf
        if elf.tag(11) != 24:
            raise ElfError("unsupported symbol entry size")
        count, source = elf.symbol_count()
        table = elf.address(elf.tag(6), count * 24)
        versions = elf.address(elf.tag(0x6FFFFFF0), count * 2) if 0x6FFFFFF0 in elf.tags else None
        result = []
        for index in range(count):
            name, info, visibility, section, value, size = elf.unpack("IBBHQQ", table + index * 24)
            version_id = elf.unpack("H", versions + index * 2)[0] if versions is not None else 1
            version_index = version_id & 0x7FFF
            mapping = self.definitions if section else self.requirements
            version = mapping.get(version_index)
            if version_index > 1 and version is None:
                raise ElfError("unmapped symbol version")
            result.append({"index": index, "name": self.name(name), "binding": info >> 4,
                           "type": info & 15, "visibility": visibility & 3, "defined": section != 0,
                           "version_index": version_index, "version_hidden": bool(version_id & 0x8000),
                           "version": version if version_index > 1 else None})
        return result

    def relocations(self):
        elf = self.elf
        tables = []
        for name, address_tag, size_tag, entry_tag, entry_size, layout in [
            ("RELA", 7, 8, 9, 24, "QQq"), ("REL", 17, 18, 19, 16, "QQ")]:
            if address_tag in elf.tags or size_tag in elf.tags:
                if elf.tag(entry_tag) != entry_size:
                    raise ElfError("invalid relocation entry size")
                tables.append((name, elf.tag(address_tag), elf.tag(size_tag), entry_size, layout))
        if 23 in elf.tags or 2 in elf.tags:
            kind = elf.tag(20)
            if kind not in {7, 17}:
                raise ElfError("unknown PLT relocation encoding")
            tables.append(("JMPREL", elf.tag(23), elf.tag(2), 24 if kind == 7 else 16,
                           "QQq" if kind == 7 else "QQ"))
        count, source = elf.symbol_count()
        result = []
        references = Counter()
        reference_types = defaultdict(Counter)
        for name, virtual, size, stride, layout in tables:
            if size % stride:
                raise ElfError("misaligned relocation table size")
            offset = elf.address(virtual, size)
            kinds = Counter()
            invalid_targets = 0
            invalid_spans = 0
            unknown_widths = 0
            for position in range(offset, offset + size, stride):
                row = elf.unpack(layout, position)
                target, info = row[:2]
                symbol, kind = info >> 32, info & 0xFFFFFFFF
                if symbol >= count:
                    raise ElfError("relocation references absent symbol")
                if kind and not any(segment["kind"] == 1 and
                                    segment["virtual"] <= target < segment["virtual"] + segment["memsz"]
                                    for segment in elf.segments):
                    invalid_targets += 1
                width = RELOCATION_WIDTHS.get(kind)
                if kind and width is None:
                    unknown_widths += 1
                elif width and not any(segment["kind"] == 1 and
                                       segment["virtual"] <= target and
                                       target + width <= segment["virtual"] + segment["memsz"]
                                       for segment in elf.segments):
                    invalid_spans += 1
                kinds[kind] += 1
                if symbol:
                    references[symbol] += 1
                    reference_types[symbol][str(kind)] += 1
            result.append({"table": name, "count": size // stride,
                           "types": {str(kind): {"name": RELOCATION_TYPES.get(kind, "UNKNOWN"), "count": total}
                                     for kind, total in sorted(kinds.items())},
                           "targets_outside_load_segments": invalid_targets,
                           "known_width_spans_outside_load_segments": invalid_spans,
                           "unknown_width_relocations": unknown_widths})
        undecoded = [name for tag, name in [(0x6000000F, "ANDROID_REL"), (0x60000011, "ANDROID_RELA"),
                                           (0x6FFFE000, "ANDROID_RELR"), (36, "RELR")]
                     if tag in elf.tags]
        return {"tables": result, "symbol_reference_counts": dict(references),
                "symbol_reference_types": {index: dict(kinds) for index, kinds in reference_types.items()},
                "counts_are_per_table_not_deduplicated": True,
                "undecoded_encodings": undecoded, "applied": False}

    def startup(self):
        arrays = {}
        for name, address, size in [("init_array", 25, 27), ("fini_array", 26, 28),
                                    ("preinit_array", 32, 33)]:
            if address in self.elf.tags or size in self.elf.tags:
                length = self.elf.tag(size)
                if length % 8:
                    raise ElfError("invalid initializer array size")
                self.elf.address(self.elf.tag(address), length)
                arrays[name + "_slots"] = length // 8
        return {**arrays, "dt_init_present": 12 in self.elf.tags,
                "dt_fini_present": 13 in self.elf.tags,
                "constructors_executed": False, "array_entries_are_not_coverage": True}


def candidates(imported, exported):
    if imported["defined"] or imported["binding"] not in {1, 2} or imported["version_index"] == 0:
        return False
    if not exported["defined"] or exported["binding"] not in {1, 2, 10} or exported["visibility"] not in {0, 3}:
        return False
    if imported["name"] != exported["name"]:
        return False
    required = imported["version"]
    provided = exported["version"]
    if required:
        return bool(provided and required["name"] == provided["name"] and required["hash"] == provided["hash"])
    return not exported["version_hidden"] and exported["version_index"] != 0


def inspect(root, supported):
    identity = audit(root, supported)
    exports = defaultdict(list)
    parsed = {}
    for relative, metadata in identity["libraries"].items():
        path = member(root, relative)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError("input changed after identity audit")
        library = LoaderMetadata(Elf64(data))
        symbols = library.symbols()
        parsed[relative] = (library, symbols)
        for symbol in symbols:
            if symbol["defined"] and symbol["name"]:
                exports[symbol["name"]].append((relative, symbol))
    target, symbols = parsed["app-libs/libg.so"]
    relocations = target.relocations()
    imports = []
    for symbol in symbols:
        if symbol["defined"] or not symbol["name"]:
            continue
        matches = [{"path": relative, "sha256": identity["libraries"][relative]["sha256"],
                    "symbol_type": exported["type"], "version": exported["version"]}
                   for relative, exported in exports[symbol["name"]] if candidates(symbol, exported)]
        imports.append({**symbol, "relocation_references": relocations["symbol_reference_counts"].get(symbol["index"], 0),
                        "relocation_types": relocations["symbol_reference_types"].get(symbol["index"], {}),
                        "static_candidates": matches,
                        "status": "STATIC_FOUND" if matches else "UNRESOLVED",
                        "runtime_binding": "NOT_RUN"})
    return {"schema": "nashura-static-loader-audit.v1", "status": "STATIC_FOUND",
            "native_game_verified": False, "runtime_load": "NOT_RUN", "namespace_resolution": "UNRESOLVED",
            "scope": "Candidate inventory only; not the runtime lookup order or proof of symbol resolution.",
            "libg_sha256": identity["libg"]["sha256"],
            "runtime_fingerprint_match": identity["fingerprint_match"],
            "manifest_matches": {name: {key: row[key] for key in ["manifest_sha256", "files", "matching"]}
                                 for name, row in identity["manifests"].items()},
            "candidate_libraries": len(parsed), "imports": imports,
            "version_requirements": list(target.requirements.values()),
            "relocations": relocations, "startup": target.startup(),
            "counts": {"imports": len(imports), "with_static_candidates": sum(bool(row["static_candidates"]) for row in imports),
                       "without_candidates": sum(not row["static_candidates"] for row in imports),
                       "weak_without_candidates": sum(not row["static_candidates"] and row["binding"] == 2 for row in imports)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--supported-engine", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    report = inspect(args.baseline, json.loads(args.supported_engine.read_text()))
    with args.output.open("x") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], **report["counts"], "runtime_load": "NOT_RUN"}))


if __name__ == "__main__":
    main()
