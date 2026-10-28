import hashlib
import json

import pytest

from materialize_update_subset import digest, materialize


def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a").write_bytes(b"first")
    (source / "b").write_bytes(b"second")
    values = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in ("a", "b")}
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([values["a"] + "  ./a"]))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"assets": {}, "data": {}, "search": values, "update": values}))
    return source, inventory, manifest


def test_copies_only_verified_subset_without_changing_source(tmp_path):
    source, inventory, manifest = fixture(tmp_path)
    output = tmp_path / "output"
    result = materialize(source, inventory, digest(inventory), manifest, digest(manifest), output, 0)
    assert result["files"] == 1
    assert (output / "resources" / "a").read_bytes() == b"first"
    assert not (output / "resources" / "b").exists()
    assert (source / "b").read_bytes() == b"second"
    with pytest.raises(ValueError, match="destination"):
        materialize(source, inventory, digest(inventory), manifest, digest(manifest), output, 0)


def test_rejects_tampering_and_symlink(tmp_path):
    source, inventory, manifest = fixture(tmp_path)
    (source / "a").write_bytes(b"modified")
    with pytest.raises(ValueError, match="identity"):
        materialize(source, inventory, digest(inventory), manifest, digest(manifest), tmp_path / "one", 0)
    (source / "a").unlink()
    (source / "a").symlink_to(source / "b")
    with pytest.raises(ValueError, match="symlink"):
        materialize(source, inventory, digest(inventory), manifest, digest(manifest), tmp_path / "two", 0)


def test_rejects_different_namespace_and_insufficient_space(tmp_path):
    source, inventory, manifest = fixture(tmp_path)
    with pytest.raises(ValueError, match="free space"):
        materialize(source, inventory, digest(inventory), manifest, digest(manifest), tmp_path / "one", 1 << 80)
    original = json.loads(manifest.read_text())
    original["search"] = {}
    manifest.write_text(json.dumps(original))
    with pytest.raises(ValueError, match="manifest"):
        materialize(source, inventory, digest(inventory), manifest, digest(manifest), tmp_path / "two", 0)
