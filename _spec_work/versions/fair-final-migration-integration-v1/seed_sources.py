import hashlib
import json
from pathlib import Path

from migration_series import validate_engine_seed
from native_runner.training.fair_final.contracts import exact_fields
from native_runner.training.fair_search.contracts import require_sha, semantic_digest


def read_artifacts(paths):
    artifacts = {}
    for path in paths:
        data = Path(path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest in artifacts:
            raise ValueError("duplicate seed source artifact")
        artifacts[digest] = data
    return artifacts


def pointer_value(document, pointer):
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("explicit JSON pointer required for seed source")
    current = document
    if not pointer:
        return current
    for token in pointer[1:].split("/"):
        decoded = token.replace("~1", "/").replace("~0", "~")
        if decoded.replace("~", "~0").replace("/", "~1") != token:
            raise ValueError("noncanonical seed source JSON pointer")
        if isinstance(current, dict) and decoded in current:
            current = current[decoded]
        elif isinstance(current, list) and decoded.isascii() and decoded.isdecimal():
            index = int(decoded)
            if str(index) != decoded or index >= len(current):
                raise ValueError("seed source array pointer is out of range or noncanonical")
            current = current[index]
        else:
            raise ValueError("seed source JSON pointer does not resolve")
    return current


def verify_seed_sources(inventory_artifacts, *, source_map_path, source_paths):
    map_bytes = Path(source_map_path).read_bytes()
    source_map = json.loads(map_bytes)
    exact_fields(source_map, {"schema", "sources"}, "seed source map")
    if source_map["schema"] != "FairFinalSeedSources/v1" or not isinstance(source_map["sources"], list):
        raise ValueError("explicit seed source map required")
    artifacts = read_artifacts(source_paths)
    projections = {}
    for entry in source_map["sources"]:
        exact_fields(entry, {"file_sha256", "engine_seed_pointers"}, "seed source projection")
        digest = require_sha(entry["file_sha256"])
        if digest in projections or digest not in artifacts:
            raise ValueError("duplicate or missing seed source identity")
        pointers = entry["engine_seed_pointers"]
        if (not isinstance(pointers, list) or not pointers
                or any(not isinstance(pointer, str) for pointer in pointers)
                or len(set(pointers)) != len(pointers)):
            raise ValueError("unique explicit seed source pointers required")
        document = json.loads(artifacts[digest])
        seeds = set()
        for pointer in pointers:
            value = pointer_value(document, pointer)
            values = value if isinstance(value, list) else [value]
            for seed in values:
                validate_engine_seed(seed)
                seeds.add(seed)
        projections[digest] = seeds
    declared_sources = set()
    for raw in inventory_artifacts.values():
        inventory = json.loads(raw)
        declared_sources.update(inventory["source_shas"])
        expected = set()
        for digest in inventory["source_shas"]:
            if digest not in projections:
                raise ValueError("inventory seed source has no verified projection")
            expected.update(projections[digest])
        if set(inventory["engine_seeds"]) != expected:
            raise ValueError("inventory engine seeds differ from source projections")
    if set(projections) != declared_sources or set(artifacts) != declared_sources:
        raise ValueError("exact declared seed source artifact set required")
    identity = dict(map_file_sha256=hashlib.sha256(map_bytes).hexdigest(),
        map_semantic_sha=semantic_digest(source_map),
        sources=[dict(file_sha256=digest, bytes=len(artifacts[digest])) for digest in sorted(artifacts)],
        scope="provided_source_artifacts_and_explicit_json_pointers_only")
    return identity, map_bytes, artifacts
