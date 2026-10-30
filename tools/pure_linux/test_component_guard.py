"""Guard integrity and strict installation evidence; native parity is separate."""
from pathlib import Path
import re
from types import SimpleNamespace
import pytest
import run_headless_match as runner
from audit_component_guard import audit, branch_target
from test_run_headless_match import inputs, COMPLETE


@pytest.mark.parametrize("word,pc,target", [
    (0x14000002, 0x1000, 0x1008), (0x97ffffff, 0x1000, 0xffc),
    (0x54000040, 0x1000, 0x1008), (0x34ffffe0, 0x1000, 0xffc),
    (0x37ffffe0, 0x1000, 0xffc), (0xd65f03c0, 0x1000, None)])
def test_direct_entry_decoder(word, pc, target):
    assert branch_target(word, pc) == target


def test_audit_rejects_unpinned_elf(tmp_path):
    path = tmp_path / "wrong.so"
    path.write_bytes(b"not the pinned engine")
    with pytest.raises(ValueError, match="exact pinned engine"):
        audit(path)


def test_fast_path_is_target_guarded_and_fallback_replays_original_prologue():
    text = Path(__file__).with_name("headless_component_guard.h").read_text()
    def words(name):
        block = text.split("static const uint32_t " + name + "[] = {", 1)[1].split("};", 1)[0]
        return [int(x, 0) for x in re.findall(r"0x[0-9a-f]+|(?<![a-z0-9])0(?![a-z0-9])", block)]
    old, stub = words("component_guard_original"), words("component_guard_stub")
    assert len(old) == 49 and len(stub) == 14
    assert old[10] == stub[9] == 0xa9bc7bfd
    assert stub[:7] == [0x37000123, 0xb40000c4, 0xf9400089, 0xf9400929,
                        0x5800010a, 0xeb0a013f, 0x54000061]
    assert stub[7:9] == [0x52800020, 0xd65f03c0]
    assert "memcmp((void *)(engine + 0x115d540), expected, bytes)" in text
    assert "engine+0x115d56c" in text
    assert "PROT_READ|PROT_WRITE|PROT_EXEC" not in text
    assert "concurrent mutation are outside the contract" in text


@pytest.mark.parametrize("index", range(5))
def test_combination_requires_all_ordered_child_markers(inputs, monkeypatch, index):
    settings, output = inputs
    markers = runner.optimization_installation_markers("hot-distance-component-v1")
    assert len(markers) == 5
    markers.pop(index)
    def execute(command, *, env, stdout, stderr):
        stdout.write((chr(10).join(markers) + chr(10) + COMPLETE).encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, "run", execute)
    with pytest.raises(ValueError, match="installation evidence"):
        runner.run(settings, output, apply=True, snapshot_trace=False,
                   native_optimization="hot-distance-component-v1")
