import pytest

from preference_backend_job import compare_entries, entries


@pytest.mark.parametrize("actual,valid", [
    ({"label": "new", "other": "keep"}, True),
    ({"label": "old", "other": "keep", "new": "value"}, True),
    ({"label": "old", "other": "keep"}, False),
    ({"label": "new", "other": "changed"}, False),
    ({"label": "new"}, False),
    ({"label": "new", "other": "keep", "new": "value"}, False),
])
def test_one_write_preserves_unrelated_entries(actual, valid):
    assert compare_entries({"label": "old", "other": "keep"}, actual)["unrelated_entries_preserved"] is valid


@pytest.mark.parametrize("raw", [b"<map><int name='a'/></map>",
                               b"<map><string name='a'/><string name='a'/></map>",
                               b"<map><string name='a'><child/></string></map>",
                               b"<map attribute='value'/>"])
def test_xml_comparison_rejects_non_string_or_duplicate(tmp_path, raw):
    path = tmp_path / "input.xml"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        entries(path)
