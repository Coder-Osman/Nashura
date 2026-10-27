"""Synthetic loader metadata checks; no native game execution."""
import struct
import unittest

from elf_audit import Elf64, ElfError
from loader_audit import LoaderMetadata, candidates
from test_audit import fixture


def versioned_fixture():
    content = fixture()
    content.extend(bytes(4096 - len(content)))
    struct.pack_into("<QQ", content, 96, len(content), len(content))
    strings = b"\0libc.so\0JNI_OnLoad\0AAsset_read\0LIBC\0ENGINE\0"
    content[2048:2048 + len(strings)] = strings
    tags = [(1, 1), (5, 2048), (10, len(strings)), (6, 704), (11, 24), (4, 800),
            (0x6FFFFFF0, 1024), (0x6FFFFFFE, 1100), (0x6FFFFFFF, 1),
            (0x6FFFFFFC, 1200), (0x6FFFFFFD, 1),
            (7, 1400), (8, 48), (9, 24), (25, 1500), (27, 16), (0, 0)]
    struct.pack_into("<QQ", content, 152, len(tags) * 16, len(tags) * 16)
    for index, entry in enumerate(tags):
        struct.pack_into("<QQ", content, 256 + index * 16, *entry)
    struct.pack_into("<HHH", content, 1024, 0, 3, 2)
    struct.pack_into("<HHIII", content, 1100, 1, 1, 1, 16, 0)
    struct.pack_into("<IHHII", content, 1116, 0x123, 0, 2, strings.index(b"LIBC"), 0)
    struct.pack_into("<HHHHIII", content, 1200, 1, 0, 3, 1, 0x456, 20, 0)
    struct.pack_into("<II", content, 1220, strings.index(b"ENGINE"), 0)
    struct.pack_into("<QQq", content, 1400, 1600, (2 << 32) | 1025, 0)
    struct.pack_into("<QQq", content, 1424, 1608, 1027, 900)
    return content


class LoaderAuditTests(unittest.TestCase):
    def test_sectionless_version_requirements_and_exports(self):
        metadata = LoaderMetadata(Elf64(versioned_fixture()))
        symbols = metadata.symbols()
        self.assertEqual(symbols[1]["version"]["name"], "ENGINE")
        self.assertEqual(symbols[2]["version"]["name"], "LIBC")
        self.assertEqual(symbols[2]["version"]["file"], "libc.so")
        self.assertEqual(symbols[2]["version"]["hash"], 0x123)
        self.assertEqual(metadata.elf.section_count, 0)

    def test_reject_unmapped_version(self):
        content = versioned_fixture()
        struct.pack_into("<H", content, 1028, 17)
        with self.assertRaises(ElfError):
            LoaderMetadata(Elf64(content)).symbols()

    def test_reject_incomplete_version_tags(self):
        elf = Elf64(versioned_fixture())
        del elf.tags[0x6FFFFFFF]
        with self.assertRaises(ElfError):
            LoaderMetadata(elf)

    def test_reject_chain_count_mismatch(self):
        for count, following in [(2, 0), (1, 32)]:
            with self.subTest(count=count, following=following):
                content = versioned_fixture()
                struct.pack_into("<H", content, 1102, count)
                struct.pack_into("<I", content, 1128, following)
                with self.assertRaises(ElfError):
                    LoaderMetadata(Elf64(content))

    def test_reject_bad_version_string(self):
        content = versioned_fixture()
        struct.pack_into("<I", content, 1124, 4096)
        with self.assertRaises(ElfError):
            LoaderMetadata(Elf64(content))

    def test_named_version_requires_name_and_hash(self):
        imported = LoaderMetadata(Elf64(versioned_fixture())).symbols()[2]
        exported = {**imported, "defined": True}
        self.assertTrue(candidates(imported, exported))
        for version in [None, {"name": "LIBC", "hash": 0x124}, {"name": "OTHER", "hash": 0x123}]:
            with self.subTest(version=version):
                self.assertFalse(candidates(imported, {**exported, "version": version}))

    def test_visibility_binding_and_default_version(self):
        imported = LoaderMetadata(Elf64(versioned_fixture())).symbols()[2]
        exported = {**imported, "defined": True}
        for changes in [{"defined": False}, {"binding": 0}, {"visibility": 2}, {"visibility": 1}]:
            self.assertFalse(candidates(imported, {**exported, **changes}))
        self.assertTrue(candidates(imported, {**exported, "visibility": 3}))
        self.assertTrue(candidates(imported, {**exported, "version_hidden": True}))
        self.assertFalse(candidates({**imported, "version": None}, {**exported, "version_hidden": True}))
        self.assertTrue(candidates({**imported, "version": None}, exported))
        for changes in [{"defined": True}, {"binding": 0}, {"version_index": 0}]:
            self.assertFalse(candidates({**imported, **changes}, exported))

    def test_reject_other_architecture(self):
        content = versioned_fixture()
        struct.pack_into("<H", content, 18, 62)
        with self.assertRaises(ElfError):
            LoaderMetadata(Elf64(content))

    def test_relocation_symbol_refs_and_memory_targets(self):
        metadata = LoaderMetadata(Elf64(versioned_fixture()))
        relocations = metadata.relocations()
        self.assertEqual(relocations["symbol_reference_counts"], {2: 1})
        self.assertEqual(relocations["tables"][0]["types"]["1025"]["count"], 1)
        self.assertEqual(relocations["tables"][0]["types"]["1027"]["count"], 1)
        self.assertEqual(relocations["tables"][0]["targets_outside_load_segments"], 0)
        self.assertFalse(relocations["applied"])
        self.assertEqual(relocations["symbol_reference_types"], {2: {"1025": 1}})

    def test_relocation_full_span_and_tls_descriptor(self):
        for kind, target, invalid in [(1025, 4088, 0), (1025, 4089, 1),
                                      (1031, 4080, 0), (1031, 4081, 1)]:
            with self.subTest(kind=kind, target=target):
                content = versioned_fixture()
                struct.pack_into("<QQ", content, 1400, target, (2 << 32) | kind)
                table = LoaderMetadata(Elf64(content)).relocations()["tables"][0]
                self.assertEqual(table["known_width_spans_outside_load_segments"], invalid)
                self.assertEqual(table["targets_outside_load_segments"], 0)

    def test_reject_absent_symbol_in_relocation(self):
        content = versioned_fixture()
        struct.pack_into("<Q", content, 1408, (3 << 32) | 1025)
        with self.assertRaises(ElfError):
            LoaderMetadata(Elf64(content)).relocations()

    def test_bad_target_and_unknown_type_remain_visible(self):
        content = versioned_fixture()
        struct.pack_into("<QQ", content, 1400, 99999, (2 << 32) | 1234)
        table = LoaderMetadata(Elf64(content)).relocations()["tables"][0]
        self.assertEqual(table["targets_outside_load_segments"], 1)
        self.assertEqual(table["types"]["1234"]["name"], "UNKNOWN")
        self.assertEqual(table["unknown_width_relocations"], 1)

    def test_packed_encoding_explicitly_undecoded(self):
        elf = Elf64(versioned_fixture())
        elf.tags[0x60000011] = [1800]
        relocations = LoaderMetadata(elf).relocations()
        self.assertEqual(relocations["undecoded_encodings"], ["ANDROID_RELA"])

    def test_startup_array_not_runtime_coverage(self):
        metadata = LoaderMetadata(Elf64(versioned_fixture()))
        startup = metadata.startup()
        self.assertEqual(startup["init_array_slots"], 2)
        self.assertFalse(startup["constructors_executed"])
        metadata.elf.tags[27] = [7]
        with self.assertRaises(ElfError):
            metadata.startup()


if __name__ == "__main__":
    unittest.main()
