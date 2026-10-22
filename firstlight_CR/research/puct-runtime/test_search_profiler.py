from search_profile import SearchProfile


def test_profile_summary_is_scoped_to_one_root():
    first = SearchProfile(root_id="episode:0", simulation_count=32)
    first.append("restore_rpc_ms", 4.0, 1)
    first.append("advance_wall_ms", 9.0, 1)
    second = SearchProfile(root_id="episode:1", simulation_count=32)
    second.append("restore_rpc_ms", 2.0, 1)

    first_summary = first.summary()
    second_summary = second.summary()
    assert first_summary["root_id"] == "episode:0"
    assert first_summary["simulation_count"] == 32
    assert first_summary["rss_peak_mb"] > 0
    assert first_summary["exclusive_spans_ms"] == {
        "restore_rpc_ms": 4.0,
        "advance_wall_ms": 9.0,
    }
    assert second_summary["exclusive_spans_ms"] == {"restore_rpc_ms": 2.0}


def test_profile_summary_includes_named_counters():
    profile = SearchProfile(root_id="episode:counter", simulation_count=32)
    profile.count("edge_traversals", 3)
    profile.set_max("max_search_depth", 2)

    assert profile.summary()["counters"] == {
        "edge_traversals": 3,
        "max_search_depth": 2,
    }
