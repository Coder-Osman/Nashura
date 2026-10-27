import pytest

from preference_initializer_audit import initializer_graph
from preference_method_audit import INITIALIZER


def fixture(target=8, bypass="return-void", boundary="p", catches="(none)"):
    return f"""    #0 : (in Lfixture;)
      access        : 0x0001 (PUBLIC)
      registers     : 2
      ins           : 1
      outs          : 1
      insns size    : {target + 1} 16-bit code units
001000: |[001000] {INITIALIZER}
001010: |0000: if-nez v0, {target:04x} // +{target:04x}
001014: |0002: const-string v0, "PRIVATE_FIXTURE"
001018: |0004: iput-object v0, v1, Lcom/supercell/titan/GameApp;.{boundary}:Lc/a;
00101c: |0006: invoke-static {{}}, Lfixture;.notCovered:()V
001020: |{target:04x}: {bypass}
      catches       : {catches}
      positions     :
"""


def test_relocation_resolves_actual_bypass_instead_of_discarding_offset():
    original = initializer_graph(fixture(), b"")
    shifted = initializer_graph(fixture(target=10), b"")
    assert original == shifted
    assert original["reachable_nodes"] == 4
    assert original["terminals"] == {"initialized": 1, "return": 1, "throw": 0}
    assert "PRIVATE_FIXTURE" not in str(original)


def test_changed_skip_path_or_argument_detected():
    original = initializer_graph(fixture(), b"")
    assert original != initializer_graph(fixture(bypass="throw v1"), b"")
    assert original != initializer_graph(fixture().replace("if-nez v0", "if-eqz v0"), b"")
    assert original != initializer_graph(fixture().replace('"PRIVATE_FIXTURE"', '"OTHER"'), b"")


@pytest.mark.parametrize("damage", ["bad_target", "bad_delta", "catches", "missing_boundary", "switch"])
def test_unresolved_control_flow_is_rejected(damage):
    altered = {
        "bad_target": fixture().replace("0008 // +0008", "0001 // +0001"),
        "bad_delta": fixture().replace("0008 // +0008", "0008 // +0007"),
        "catches": fixture(catches="1"),
        "missing_boundary": fixture(boundary="other"),
        "switch": fixture().replace("if-nez", "packed-switch"),
    }[damage]
    with pytest.raises(ValueError):
        initializer_graph(altered, b"")


def test_loop_topology_preserved_and_cold_continuation_excluded():
    source = fixture().replace("0002: const-string v0, \"PRIVATE_FIXTURE\"",
                               "0002: if-eqz v1, 0002 // +0000")
    result = initializer_graph(source, b"")
    assert result["resolved_branches"] == 2
    assert result == initializer_graph(source.replace("notCovered", "differentAfterBoundary"), b"")
    assert result != initializer_graph(source.replace("0002 // +0000", "0000 // -0002"), b"")
