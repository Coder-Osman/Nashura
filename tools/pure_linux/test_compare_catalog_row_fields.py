import pytest

from compare_catalog_row_fields import SLOTS, grouped


def test_grouped_requires_exact_order_and_coverage():
    sizes = {slot: 0 for slot in SLOTS}
    sizes[9] = 2
    assert grouped([[9, 0, 11], [9, 1, 12]], sizes)[9] == [11, 12]
    with pytest.raises(ValueError, match="order"):
        grouped([[9, 1, 11], [9, 0, 12]], sizes)
    with pytest.raises(ValueError, match="coverage"):
        grouped([[9, 0, 11]], sizes)
