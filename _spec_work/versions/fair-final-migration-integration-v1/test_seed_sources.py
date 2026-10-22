from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT.parent / "fair-final-migration-series-v1"),
    str(ROOT.parents[2] / "firstlight_CR")]

from seed_registration import archive_registration, make_registration
from seed_sources import pointer_value, verify_seed_sources
from test_seed_registration import registration_case, validate
from native_runner.training.fair_final.storage import json_bytes
from native_runner.training.fair_search.contracts import semantic_digest


def build(case):
    return make_registration(case["plan"], expected_plan_sha=semantic_digest(case["plan"]),
        inventory_paths=case["paths"], source_map_path=case["source_map"], source_paths=case["sources"])


def rewrite_source(case, document, pointers):
    source = case["sources"][0]
    old = hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_bytes(json_bytes(document))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    mapping = json.loads(case["source_map"].read_bytes())
    entry = next(entry for entry in mapping["sources"] if entry["file_sha256"] == old)
    entry.update(file_sha256=digest, engine_seed_pointers=pointers)
    case["source_map"].write_bytes(json_bytes(mapping))
    inventory = json.loads(case["paths"][0].read_bytes())
    inventory["source_shas"] = [digest]
    case["paths"][0].write_bytes(json_bytes(inventory))


def test_sources_relocated_and_reordered_preserve_registration(registration_case, tmp_path):
    case = registration_case
    before = deepcopy(case["registration"])
    copied = []
    for index, path in enumerate(reversed(case["sources"])):
        target = tmp_path / (str(index) + "-relocated.json")
        target.write_bytes(path.read_bytes())
        copied.append(target)
    case["sources"] = copied
    assert build(case) == before
    assert validate(case)["seed_source_map_sha"] == before["source_evidence"]["map_file_sha256"]


def test_projection_covers_all_registered_source_seeds(registration_case):
    case = registration_case
    rewrite_source(case, {"private_tasks": [{"engine_seed": 7000}, {"engine_seed": 8000}]},
        ["/private_tasks/0/engine_seed", "/private_tasks/1/engine_seed"])
    with pytest.raises(ValueError, match="differ from source projections"):
        build(case)
    inventory = json.loads(case["paths"][0].read_bytes())
    inventory["engine_seeds"].append(8000)
    case["paths"][0].write_bytes(json_bytes(inventory))
    case["plan"]["excluded_engine_seeds"].append(8000)
    assert build(case)["source_evidence"]["sources"]


@pytest.mark.parametrize("damage", ["source_bytes", "missing_source", "extra_source", "duplicate_source",
    "missing_projection", "duplicate_projection", "unknown_projection", "extra_map_field", "bad_schema",
    "empty_pointers", "duplicate_pointers", "pointer_type", "unresolved_pointer", "map_bytes"])
def test_bound_source_evidence_rejects_tampering(registration_case, tmp_path, damage):
    case = registration_case
    mapping = json.loads(case["source_map"].read_bytes())
    if damage == "source_bytes":
        case["sources"][0].write_bytes(case["sources"][0].read_bytes() + b"\n")
    elif damage == "missing_source":
        case["sources"].pop()
    elif damage in ("extra_source", "duplicate_source"):
        extra = tmp_path / "extra-source.json"
        extra.write_bytes(b"{}" if damage == "extra_source" else case["sources"][0].read_bytes())
        case["sources"].append(extra)
    elif damage == "missing_projection":
        mapping["sources"].pop()
    elif damage == "duplicate_projection":
        mapping["sources"].append(deepcopy(mapping["sources"][0]))
    elif damage == "unknown_projection":
        mapping["sources"][0]["file_sha256"] = "f" * 64
    elif damage == "extra_map_field":
        mapping["unexpected"] = True
    elif damage == "bad_schema":
        mapping["schema"] = "unknown"
    elif damage == "map_bytes":
        case["source_map"].write_bytes(case["source_map"].read_bytes() + b"\n")
    else:
        mapping["sources"][0]["engine_seed_pointers"] = {
            "empty_pointers": [], "duplicate_pointers": ["/engine_seeds", "/engine_seeds"],
            "pointer_type": [False], "unresolved_pointer": ["/missing"]}[damage]
    if damage not in ("source_bytes", "missing_source", "extra_source", "duplicate_source", "map_bytes"):
        case["source_map"].write_bytes(json_bytes(mapping))
    with pytest.raises(ValueError):
        validate(case)


@pytest.mark.parametrize("seed", [True, -1, 1 << 32, 7000.0, "7000", None, {}, [[7000]]])
def test_projection_requires_canonical_engine_seeds(registration_case, seed):
    case = registration_case
    rewrite_source(case, {"engine_seeds": seed}, ["/engine_seeds"])
    with pytest.raises(ValueError):
        build(case)


@pytest.mark.parametrize("pointer", ["missing", "/missing", "/~2", "/items/00", "/items/-1",
    "/items/1", "/items/١", "/items/0/value"])
def test_projection_rejects_invalid_pointers(pointer):
    with pytest.raises(ValueError):
        pointer_value({"items": [7000]}, pointer)


def test_projection_supports_escaped_keys_and_repeated_seat_seeds(registration_case):
    case = registration_case
    rewrite_source(case, {"a/b": {"~key": [7000, 7000]}}, ["/a~1b/~0key"])
    assert build(case)["registered_round"] == 2


@pytest.mark.parametrize("change", ["source", "mapping"])
def test_source_change_before_archive_leaves_no_published_artifacts(registration_case, tmp_path, change):
    case = registration_case
    validate(case)
    path = case["sources"][0] if change == "source" else case["source_map"]
    path.write_bytes(path.read_bytes() + b"\n")
    output = tmp_path / "audit"
    with pytest.raises(ValueError):
        archive_registration(case["registration"], expected_sha=semantic_digest(case["registration"]),
            inventory_paths=case["paths"], source_map_path=case["source_map"],
            source_paths=case["sources"], output=output)
    assert not output.exists()


def test_source_verifier_returns_exact_archived_bytes(registration_case):
    case = registration_case
    inventory = {hashlib.sha256(path.read_bytes()).hexdigest(): path.read_bytes() for path in case["paths"]}
    identity, mapping, sources = verify_seed_sources(inventory,
        source_map_path=case["source_map"], source_paths=case["sources"])
    assert identity == case["registration"]["source_evidence"]
    assert mapping == case["source_map"].read_bytes()
    assert set(sources.values()) == {path.read_bytes() for path in case["sources"]}
