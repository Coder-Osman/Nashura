import json

import pytest

from compare_catalog_layout import compare, sha


def test_detects_row_difference_and_refuses_tampering(tmp_path):
    android, linux = tmp_path / "android.json", tmp_path / "linux.log"
    android.write_text(json.dumps({"status": "RUNTIME_OBSERVED_LIMITED", "catalog_ready_pass": False,
        "layout": {"schema": "nashura.android-catalog-layout.v1", "catalogReadyPass": False,
                   "tables": [[1, 3, 4, 3, 2, 2]], "totalRows": 3}}))
    linux.write_text("PARSED_TABLE_SUMMARY\tbefore\t0\t0\nPARSED_TABLE\tafter\t1\t2\t4\t2\t2\t2\n"
                     "PARSED_TABLE_SUMMARY\tafter\t1\t2\n")
    a, b = sha(android), sha(linux)
    report = compare(android, a, linux, b)
    assert report["status"] == "DIFFERENCES" and report["differences"][0]["slot"] == 1
    linux.write_text(linux.read_text() + "changed")
    with pytest.raises(ValueError, match="identity"):
        compare(android, a, linux, b)
