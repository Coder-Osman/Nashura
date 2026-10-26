import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess

import pytest


GHIDRA = os.environ.get("GHIDRA_INSTALL_DIR")
pytestmark = pytest.mark.skipif(not GHIDRA, reason="Explicit local Ghidra installation required")
SCRIPT = Path(__file__).resolve().parents[1] / "ghidra"


def synthetic_elf(path):
    payload = bytearray(0x1120)
    identity = b"\x7fELF\x02\x01\x01" + bytes(9)
    payload[:64] = struct.pack("<16sHHIQQQIHHHHHH", identity,
        2, 183, 1, 0, 64, 0x1060, 0, 64, 56, 1, 64, 3, 2)
    payload[64:120] = struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0x400000,
        len(payload), len(payload), 0x1000)
    payload[0x1000:0x1014] = struct.pack("<5I",
        0x14000004, 0x94000003, 0x91000400, 0xd65f03c0, 0xd65f03c0)
    names = b"\0.text\0.shstrtab\0"
    payload[0x1020:0x1020 + len(names)] = names
    payload[0x10a0:0x10e0] = struct.pack("<IIQQQQIIQQ",
        1, 1, 6, 0x401000, 0x1000, 20, 0, 0, 4, 0)
    payload[0x10e0:0x1120] = struct.pack("<IIQQQQIIQQ",
        7, 3, 0, 0, 0x1020, len(names), 0, 0, 1, 0)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def run_export(directory, binary, digest, roots, callees, bound=False):
    directory.mkdir()
    project = directory / "project"
    project.mkdir()
    (directory / "roots.tsv").write_text(roots)
    (directory / "callees.tsv").write_text(callees)
    arguments = [str(Path(GHIDRA) / "support/analyzeHeadless"),
        str(project), "synthetic", "-import", str(binary), "-noanalysis",
        "-scriptPath", str(SCRIPT), "-postScript", "ExportBoundedFunctions.java",
        digest, str(directory / "roots.tsv"), str(directory / "export"),
        str(directory / "callees.tsv")]
    if bound:
        arguments.append("bound-terminal-calls")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=120)
    (directory / "command.json").write_text(json.dumps(arguments))
    (directory / "headless.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    return directory / "export", result.stdout + result.stderr


def test_boundary_flow_differential_without_binary_patch(tmp_path):
    binary = tmp_path / "synthetic.elf"
    digest = synthetic_elf(binary)
    roots = "Tail\t0x401000\t4\nCall\t0x401004\t4\n"
    callees = "Destination\t0x401010\t4\n"
    default, _ = run_export(tmp_path / "default", binary, digest, roots, callees)
    bounded, _ = run_export(tmp_path / "bounded", binary, digest, roots, callees, True)
    for name in ("Tail", "Call"):
        before = json.loads((default / (name + ".json")).read_text())
        after = json.loads((bounded / (name + ".json")).read_text())
        assert before["ok"] and after["ok"]
        assert before["high_pcode_outside_body"]
        assert after["high_pcode_outside_body"] == []
        assert before["assembly"] == after["assembly"]
        assert after["database_flow_overrides"] == [after["address"]]
        assert after["declarations_sha256"] == hashlib.sha256(callees.encode()).hexdigest()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("digest_override,callees,error", [
    ("0" * 64, "", "Imported binary fingerprint mismatch"),
    (None, "Overlap\t0x401000\t4\n", "Existing function overlaps"),
    (None, "Outside\t0x800000\t4\n", "Extent is not file-backed"),
])
def test_reject_invalid_inputs_before_export(tmp_path, digest_override, callees, error):
    binary = tmp_path / "synthetic.elf"
    digest = synthetic_elf(binary)
    exported, log = run_export(tmp_path / "invalid", binary, digest_override or digest,
        "Root\t0x401000\t4\n", callees)
    assert error in log
    assert not list(exported.glob("*.json"))
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == digest
