"""Synthetic parser and runner tests; none constitute native game evidence."""
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from elf_audit import Elf64, ElfError
from run_arm64_smoke import EXPECTED, validate
import run_arm64_smoke
from audit_inputs import audit, classify_candidates, member, verify_manifest


def fixture(gnu=False):
    content = bytearray(1024)
    content[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<HHIQQQIHHHHHH", content, 16, 3, 183, 1, 0, 64, 0, 0, 64, 56, 2, 0, 0, 0)
    struct.pack_into("<IIQQQQQQ", content, 64, 1, 5, 0, 0, 0, len(content), len(content), 4096)
    strings = b"\0libc.so\0JNI_OnLoad\0AAsset_read\0"
    content[600:600 + len(strings)] = strings
    struct.pack_into("<IBBHQQ", content, 704 + 24, 9, 18, 0, 1, 900, 4)
    struct.pack_into("<IBBHQQ", content, 704 + 48, 20, 18, 0, 0, 0, 0)
    tags = [(1, 1), (5, 600), (10, len(strings)), (6, 704), (11, 24)]
    if gnu:
        struct.pack_into("<IIIIQIII", content, 800, 1, 1, 1, 5, 0, 1, 2, 3)
        tags.append((0x6FFFFEF5, 800))
    else:
        struct.pack_into("<II", content, 800, 1, 3)
        tags.append((4, 800))
    tags.append((0, 0))
    struct.pack_into("<IIQQQQQQ", content, 120, 2, 6, 256, 256, 0, len(tags) * 16, len(tags) * 16, 8)
    for index, entry in enumerate(tags):
        struct.pack_into("<QQ", content, 256 + index * 16, *entry)
    return content


class AuditTests(unittest.TestCase):
    def test_candidates_exclude_other_abi_without_hiding_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arm32, arm64 = root / "arm32", root / "arm64"
            wrong = fixture()
            wrong[4] = 1
            struct.pack_into("<H", wrong, 18, 40)
            arm32.write_bytes(wrong)
            arm64.write_bytes(fixture())
            records, compatible = classify_candidates(root, [arm32, arm64])
            self.assertEqual(compatible, [arm64])
            self.assertFalse(records["arm32"]["aarch64_elf64"])
            self.assertEqual(len(records), 2)

    def test_distinct_same_abi_candidates_remain_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.write_bytes(fixture())
            different = fixture()
            different[-1] = 1
            second.write_bytes(different)
            records, compatible = classify_candidates(root, [first, second])
            self.assertEqual(len(compatible), 2)
            self.assertEqual(len({row["sha256"] for row in records.values()}), 2)

    def test_manifest_identity_and_missing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "present").write_bytes(b"synthetic resource")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"files": [
                {"path": "present", "sha256": hashlib.sha256(b"synthetic resource").hexdigest()},
                {"path": "missing", "sha256": "0" * 64}]}))
            report = verify_manifest(root, manifest)
            self.assertEqual((report["files"], report["matching"]), (2, 1))
            self.assertIsNone(report["records"][1]["actual_sha256"])

    def test_manifest_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            for relative in ["../outside", "/absolute"]:
                with self.subTest(relative=relative), self.assertRaises(ValueError):
                    member(Path(directory), relative)

    def test_engine_hash_gate_precedes_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app-libs").mkdir()
            (root / "app-libs/libg.so").write_bytes(b"synthetic wrong binary")
            with patch("audit_inputs.Elf64") as parser, self.assertRaises(ValueError):
                audit(root, {"libg_sha256": "0" * 64})
            parser.assert_not_called()

    def test_sectionless_sysv(self):
        report = Elf64(fixture()).report()
        self.assertEqual(report["section_count"], 0)
        self.assertEqual(report["needed"], ["libc.so"])
        self.assertEqual(report["defined_jni_exports"], ["JNI_OnLoad"])
        self.assertEqual(report["undefined_symbols"], ["AAsset_read"])
        self.assertIsNone(report["interpreter"])
        self.assertEqual(report["runtime_symbol_resolution"], "NOT_RUN")

    def test_sectionless_gnu(self):
        report = Elf64(fixture(True)).report()
        self.assertEqual(report["symbol_count_source"], "DT_GNU_HASH")
        self.assertEqual(report["dynamic_symbol_count"], 3)
        self.assertEqual(report["undefined_symbols"], ["AAsset_read"])

    def test_reject_malformed(self):
        for data in [b"", b"\x7fELF", fixture()[:300]]:
            with self.subTest(size=len(data)), self.assertRaises(ElfError):
                Elf64(data).report()

    def test_reject_bad_string(self):
        data = fixture()
        struct.pack_into("<I", data, 752, 1000)
        with self.assertRaises(ElfError):
            Elf64(data).report()

    def test_reject_non_file_backed_pointer(self):
        data = fixture()
        struct.pack_into("<Q", data, 280, 4096)
        with self.assertRaises(ElfError):
            Elf64(data).report()

    def test_cli_sha_mismatch_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "synthetic", Path(directory) / "report.json"
            source.write_bytes(fixture())
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("elf_audit.py")),
                                     "--input", str(source), "--sha256", "0" * 64,
                                     "--output", str(output)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_cli_exclusive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "synthetic", Path(directory) / "report.json"
            source.write_bytes(fixture())
            output.write_text("prior evidence")
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("elf_audit.py")),
                                     "--input", str(source), "--sha256", hashlib.sha256(source.read_bytes()).hexdigest(),
                                     "--output", str(output)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), "prior evidence")

    def test_smoke_requires_every_check_and_exact_exit(self):
        stdout = "\n".join(json.dumps({"check": name, "pass": True}) for name in EXPECTED)
        self.assertTrue(validate(stdout, 0))
        self.assertFalse(validate(stdout, 23))
        self.assertFalse(validate(stdout.replace('"pass": true', '"pass": false', 1), 0))
        self.assertFalse(validate("\n".join(stdout.splitlines()[:-1]), 0))
        self.assertFalse(validate(stdout + "\n{}", 0))
        self.assertFalse(validate("not-json", 0))
        self.assertFalse(validate(stdout.replace('"pass": true', '"pass": 1'), 0))

    def test_smoke_rejects_wrong_host_without_execution(self):
        for system, machine in [("Darwin", "arm64"), ("Linux", "x86_64")]:
            with self.subTest(system=system, machine=machine), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "result"
                with patch.object(sys, "argv", ["smoke", "--elf", "absent", "--sha256", "0" * 64,
                                                "--output", str(output)]), \
                     patch("run_arm64_smoke.platform.system", return_value=system), \
                     patch("run_arm64_smoke.platform.machine", return_value=machine), \
                     patch("run_arm64_smoke.subprocess.run") as execute:
                    self.assertEqual(run_arm64_smoke.main(), 2)
                    execute.assert_not_called()
                report = json.loads((output / "report.json").read_text())
                self.assertEqual(report["status"], "BLOCKED_ENVIRONMENT")
                self.assertFalse(report["native_game_verified"])

    def test_smoke_rejects_hash_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "synthetic", Path(directory) / "result"
            source.write_bytes(fixture())
            with patch.object(sys, "argv", ["smoke", "--elf", str(source), "--sha256", "0" * 64,
                                            "--output", str(output)]), \
                 patch("run_arm64_smoke.platform.system", return_value="Linux"), \
                 patch("run_arm64_smoke.platform.machine", return_value="aarch64"), \
                 patch("run_arm64_smoke.subprocess.run") as execute:
                self.assertEqual(run_arm64_smoke.main(), 2)
                execute.assert_not_called()
            self.assertEqual(json.loads((output / "report.json").read_text())["status"], "BLOCKED_INPUT")


if __name__ == "__main__":
    unittest.main()
