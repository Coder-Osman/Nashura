import pytest

from audit_matched_update_layout import audit, compare_layouts
from compare_catalog_row_fields import SLOTS


def fixture():
    lines = ["PARSED_TABLE_SUMMARY\tbefore\t0\t0"]
    lines.extend("PARSED_TABLE\tafter\t%d\t1\t1\t1\t-1\t-1" % slot for slot in SLOTS)
    lines.append("PARSED_TABLE_SUMMARY\tafter\t%d\t%d" % (len(SLOTS), len(SLOTS)))
    lines.extend("PARSED_ROW_ID\t%d\t0\t%d" % (slot, slot) for slot in SLOTS)
    tables = [[slot, 1, 1, 1, -1, -1] for slot in SLOTS]
    rows = [[slot, 0, slot] for slot in SLOTS]
    tables[0][1:4] = [2, 2, 2]
    rows.insert(1, [SLOTS[0], 1, 999])
    android = {"layout": {"schema": "nashura.android-catalog-layout.v1", "catalogReadyPass": False,
                          "tables": tables, "totalRows": len(SLOTS) + 1, "rowIds": rows}}
    return android, "\n".join(lines)


def test_detects_matched_linux_and_android_extra_row():
    android, text = fixture()
    result = compare_layouts(android, text, text)
    assert result["covered_android_only"] == 1
    assert result["covered_linux_only"] == 0
    assert result["different_slots"] == [SLOTS[0]]


def test_rejects_changed_linux_covered_value_and_missing_android_rows():
    android, text = fixture()
    changed = text.replace("PARSED_ROW_ID\t9\t0\t9", "PARSED_ROW_ID\t9\t0\t10")
    with pytest.raises(ValueError, match="row fields differ"):
        compare_layouts(android, text, changed)
    android["layout"]["rowIds"].pop()
    with pytest.raises(ValueError, match="coverage incomplete"):
        compare_layouts(android, text, text)


def test_audit_rejects_input_hash_mismatch(tmp_path):
    path = tmp_path / "evidence"
    path.write_text("{}")
    with pytest.raises(ValueError, match="identity"):
        audit({"android": path}, {"android": "0" * 64})
