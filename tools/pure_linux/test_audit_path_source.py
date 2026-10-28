import sqlite3

import pytest

from audit_path_source import compare_database


def test_compares_real_sqlite_sets(tmp_path):
    database = tmp_path / "fixture.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE meta(path TEXT)")
        connection.execute("CREATE TABLE asset(path TEXT)")
        connection.executemany("INSERT INTO meta VALUES (?)", [("a",), ("b",)])
        connection.executemany("INSERT INTO asset VALUES (?)", [("a",), ("c",)])
    rows = [{"bytes_hex": b"a".hex()}, {"bytes_hex": b"b".hex()}]
    result = compare_database(rows, database)
    assert result["meta_exact_matches"] == 2 and result["asset_exact_matches"] == 1
    assert result["meta_unobserved"] == 0 and result["observed_not_meta"] == 0


def test_repeated_path_bytes_are_not_treated_as_distinct(tmp_path):
    with pytest.raises(ValueError):
        compare_database([{"bytes_hex": "61"}, {"bytes_hex": "61"}], tmp_path / "missing")
