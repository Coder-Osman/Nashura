"""Independent synthetic checks of observation tooling, not game lifecycle parity."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from constructor_trace import summarize
from native_init_job import check_inputs, run


class ConstructorTraceTests(unittest.TestCase):
    def trace(self, text):
        return summarize(io.BytesIO(text.encode()))

    def test_actual_entry_and_return_not_array_size(self):
        row = self.trace("[ Calling DT_INIT_ARRAY (size 753) @ 0x1000 for '/private/libg.so' ]\n"
                         "[ Calling c-tor function @ 0x1004 for '/private/libg.so' ]\n"
                         "[ Done calling c-tor function @ 0x1004 for '/private/libg.so' ]\n")
        self.assertEqual(row["modules"][0], {"module": "libg.so", "array_sizes": [753],
                                             "entered": 1, "returned": 1, "unreturned": 0})

    def test_missing_return_stays_unreturned(self):
        row = self.trace("[ Calling c-tor function @ 0x123 for '/libg.so' ]\n")
        self.assertEqual(row["modules"][0]["unreturned"], 1)

    def test_wrong_module_return_is_not_a_match(self):
        row = self.trace("[ Calling c-tor function @ 0x123 for '/libg.so' ]\n"
                         "[ Done calling c-tor function @ 0x123 for '/other.so' ]\n")
        self.assertEqual(row["unexpected_returns"], 1)
        self.assertEqual(row["modules"][0]["unreturned"], 1)

    def test_nested_duplicate_entries_count_separately(self):
        enter = "[ Calling c-tor function @ 0x123 for '/libg.so' ]\n"
        leave = "[ Done calling c-tor function @ 0x123 for '/libg.so' ]\n"
        row = self.trace(enter * 2 + leave)
        self.assertEqual(row["modules"][0]["unreturned"], 1)

    def test_ifunc_observations_and_complete_hash(self):
        content = b"Called ifunc_resolver@0x10. The result is 0x20\n" * 2 + b"\xff\n"
        row = summarize(io.BytesIO(content))
        self.assertEqual(row["ifunc_calls"], 2)
        self.assertEqual(row["unique_ifunc_resolvers"], 1)
        self.assertEqual(row["log_sha256"], hashlib.sha256(content).hexdigest())

    def test_static_slot_or_unrelated_text_is_not_coverage(self):
        row = self.trace("DT_INIT_ARRAY[0] == 0x123\n753 constructors\n")
        self.assertEqual(row["modules"], [])


class NativeJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def identity(self, name, content=b"fixture"):
        (self.root / "libs").mkdir(exist_ok=True)
        (self.root / "libs" / name).write_bytes(content)
        (self.root / "input-identity.json").write_text(json.dumps({
            "libraries": {name: {"sha256": hashlib.sha256(content).hexdigest()}}}))

    def test_input_change_rejected(self):
        self.identity("libg.so")
        (self.root / "libs/libg.so").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            check_inputs(self.root)

    def test_manifest_parent_path_rejected(self):
        (self.root / "input-identity.json").write_text(json.dumps({"libraries": {"../outside": {}}}))
        with self.assertRaisesRegex(ValueError, "non-basename"):
            check_inputs(self.root)

    def test_exact_manifest_digest(self):
        self.identity("libg.so")
        self.assertEqual(check_inputs(self.root), {"libg.so": hashlib.sha256(b"fixture").hexdigest()})

    def test_real_exit_and_log_bytes(self):
        result = run([sys.executable, "-c", "print('observed');raise SystemExit(23)"],
                     self.root, os.environ.copy(), 2)
        self.assertEqual(result["exit_code"], 23)
        self.assertFalse(result["timed_out"])
        self.assertEqual((self.root / "stdout.log").read_bytes(), b"observed\n")

    def test_timeout_is_not_success(self):
        result = run([sys.executable, "-c", "import time;time.sleep(10)"],
                     self.root, os.environ.copy(), 0.05)
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["exit_code"], 0)

    def test_log_limit_terminates_noisy_child(self):
        result = run([sys.executable, "-c", "import os;os.write(1,b'x'*8192);os.write(1,b'y')"],
                     self.root, os.environ.copy(), 2, log_limit=4096)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertLessEqual((self.root / "stdout.log").stat().st_size, 4096)

    def test_existing_logs_rejected(self):
        (self.root / "stdout.log").write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            run([sys.executable, "-c", "pass"], self.root, os.environ.copy(), 2)
        self.assertEqual((self.root / "stdout.log").read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main()
