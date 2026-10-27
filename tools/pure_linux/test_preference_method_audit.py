import pytest
import struct

from preference_method_audit import INITIALIZER, array_payload, compare, initialization_prefix, method_evidence


NAME = "fixture.read:()V"
DUMP = """    #0 : (in Lfixture;)
      name          : 'read'
      type          : '()V'
      access        : 0x0001 (PUBLIC)
      code          -
      registers     : 2
      ins           : 1
      outs          : 1
      insns size    : 4 16-bit code units
001000:          |[001000] fixture.read:()V
001010: aaaa     |0000: const-string v0, "PRIVATE_FIXTURE" // string@0010
001014: bbbb     |0002: return-void
      catches       : 1
        0x0000 - 0x0002
          Ljava/lang/Exception; -> 0x0002
      positions     :
"""


def evidence(text):
    return method_evidence(text, (NAME,))[NAME]


def test_offsets_and_pool_indexes_are_not_semantic_identity():
    altered = DUMP.replace("001000", "007000").replace("001010", "007010")
    altered = altered.replace("001014", "007014").replace("string@0010", "string@0099")
    assert evidence(altered) == evidence(DUMP)
    assert "PRIVATE_FIXTURE" not in str(evidence(DUMP))


@pytest.mark.parametrize("before,after", [
    ('"PRIVATE_FIXTURE"', '"CHANGED_FIXTURE"'),
    ("registers     : 2", "registers     : 3"),
    ("outs          : 1", "outs          : 2"),
    ("0x0000 - 0x0002", "0x0001 - 0x0002"),
    ("Ljava/lang/Exception;", "Ljava/lang/RuntimeException;"),
    ("const-string v0", "const-string v1"),
])
def test_semantic_changes_are_detected(before, after):
    assert evidence(DUMP.replace(before, after)) != evidence(DUMP)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "truncated", "metadata", "instruction"])
def test_incomplete_or_ambiguous_evidence_rejected(damage):
    altered = {"missing": "", "duplicate": DUMP + DUMP,
               "truncated": DUMP.split("      catches")[0],
               "metadata": DUMP.replace("      registers     : 2\n", ""),
               "instruction": DUMP.replace("|0000: ", "|unknown: ")}[damage]
    with pytest.raises(ValueError):
        evidence(altered)


def test_initializer_difference_does_not_become_whole_closure_equivalence():
    base = evidence(DUMP)
    changed = evidence(DUMP.replace('"PRIVATE_FIXTURE"', '"CHANGED"'))
    report = compare({NAME: base, "initializer": base}, {NAME: base, "initializer": changed})
    assert [row["equal"] for row in report["methods"]] == [True, False]
    assert not report["selected_methods_equal"]
    assert report["runtime_parity"] == "NOT_RUN"
    assert not report["whole_dex_identity_equal"]
    with pytest.raises(ValueError):
        compare({NAME: base}, {})


def test_array_payload_requires_bytes_and_detects_constant_changes():
    line = "000010: 0003 |0008: array-data (6 units)"
    payload = struct.pack("<HHI", 0x0300, 1, 4) + b"abcd"
    with pytest.raises(ValueError):
        array_payload(line, None)
    assert array_payload(line, bytes(16) + payload) != array_payload(line, bytes(16) + payload[:-1] + b"z")
    for damaged in [payload[:-1], struct.pack("<HHI", 0x0300, 1, 5) + b"abcd"]:
        with pytest.raises(ValueError):
            array_payload(line, bytes(16) + damaged)


def test_initializer_prefix_retains_payload_and_excludes_later_code():
    text = f"""000000: |[000000] {INITIALIZER}
000010: aaaa |0000: fill-array-data v3, 00000008 // +00000008
000016: bbbb |0003: iput-object v3, v13, Lcom/supercell/titan/GameApp;.p:Lc/a; // field@0010
00001a: cccc |0005: return-void
000020: 0003 |0008: array-data (6 units)
      catches       : (none)
"""
    dex = bytes(32) + struct.pack("<HHI", 0x0300, 1, 4) + b"abcd"
    original = initialization_prefix(text, dex)
    assert original == initialization_prefix(text.replace("return-void", "nop"), dex)
    assert original != initialization_prefix(text, dex[:-1] + b"z")
    assert original["runtime_parity"] == "NOT_RUN"
    assert "abcd" not in str(original)
    with pytest.raises(ValueError):
        initialization_prefix(text.replace(".p:Lc/a;", ".other:Lc/a;"), dex)
