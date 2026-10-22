import json
import os

import tree_search


original_evaluate = tree_search.PuctSearch._evaluate
cache_stats = {"hits": 0, "misses": 0}


def cached_evaluate(self, state):
    key = self.state_key(state) if self.state_key is not None else id(state)
    cache = getattr(self, "_probe_leaf_value_cache", None)
    if cache is None:
        cache = {}
        self._probe_leaf_value_cache = cache
    if key in cache:
        cache_stats["hits"] += 1
        return cache[key]
    cache_stats["misses"] += 1
    value = original_evaluate(self, state)
    cache[key] = value
    return value


tree_search.PuctSearch._evaluate = cached_evaluate

import puct_vs_policy_eval as evaluation


result = evaluation.run_match(
    port=int(os.environ["PROBE_PORT"]),
    device="cuda:0",
    seed=int(os.environ["PROBE_SEED"]),
    puct_owner=0,
    simulations=32,
    max_seconds=600,
    transport="resident-hybrid",
    resident_capture="compact",
    first_root_only=True,
    profile_setup=True,
)
result["probe_leaf_cache_stats"] = cache_stats
path = os.environ["PROBE_OUTPUT"]
temporary = path + ".tmp"
with open(temporary, "w") as stream:
    json.dump(result, stream)
os.replace(temporary, path)
print(json.dumps({
    "selected_action": result["selected_action"],
    "search_seconds": result["search_seconds"],
    "fingerprint_match": result["fingerprint_match"],
    "cache_stats": cache_stats,
}))
