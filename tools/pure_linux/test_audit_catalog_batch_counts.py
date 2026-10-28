import copy

import pytest

from audit_catalog_batch_counts import BATCHES, SLOTS, compare_boundaries


def fixture():
    events, linux_events = [], []
    zero = [[slot, 0] for slot in SLOTS]
    android_last = copy.deepcopy(zero)
    linux_last = copy.deepcopy(zero)
    android_last[0][1], linux_last[0][1] = 5, 3
    for index, args in enumerate(BATCHES):
        before_a = copy.deepcopy(zero)
        before_l = copy.deepcopy(zero)
        after_a = android_last if index == 4 else copy.deepcopy(zero)
        after_l = linux_last if index == 4 else copy.deepcopy(zero)
        events.extend([{"kind": "table_batch", "call": index + 1, "thread": 1, "sequence": index * 2,
                        "args": args, "listCount": 158, "tableCounts": before_a},
                       {"kind": "table_batch_end", "call": index + 1, "thread": 1, "sequence": index * 2 + 1,
                        "tableCounts": after_a}])
        linux_events.append({"name": "table_batch", "return_x0": 0, "table_counts_before": before_l,
                             "table_counts_after": after_l})
    android_tables = [[slot, 7 if slot == SLOTS[0] else 0] for slot in SLOTS]
    android = {"trace": {"events": events}, "layout": {"catalogReadyPass": False, "tables": android_tables}}
    linux = {"exit_code": 0, "active_scopes": 0, "targets": [{"calls": 5, "armed": True,
              "module": "libg.so", "offset": 0xe1bdf4, "site_only": False,
              "table_counts_register": "x0"}], "events": linux_events}
    lines = ["PARSED_TABLE_SUMMARY\tbefore\t0\t0"]
    lines.extend("PARSED_TABLE\tafter\t%d\t%d\t%d\t%d\t-1\t-1" %
                 (slot, 4 if slot == SLOTS[0] else 0, 4 if slot == SLOTS[0] else 0,
                  4 if slot == SLOTS[0] else 0) for slot in SLOTS)
    lines.append("PARSED_TABLE_SUMMARY\tafter\t%d\t4" % len(SLOTS))
    return android, linux, "\n".join(lines)


def test_localizes_first_difference_and_post_batch_changes():
    android, linux, text = fixture()
    result = compare_boundaries(android, linux, text)
    assert result["first_divergence"]["batch"] == 4
    assert result["first_divergence"]["phase"] == "after"
    assert result["post_batch_android_delta"] == 2
    assert result["post_batch_linux_delta"] == 1


def test_rejects_unobserved_interbatch_change_and_missing_return():
    android, linux, text = fixture()
    android["trace"]["events"][2]["tableCounts"][0][1] = 1
    with pytest.raises(ValueError, match="between batches"):
        compare_boundaries(android, linux, text)
    android, linux, text = fixture()
    del linux["events"][2]["return_x0"]
    with pytest.raises(ValueError, match="returns incomplete"):
        compare_boundaries(android, linux, text)
