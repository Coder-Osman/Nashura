"""Copy an identity-bound update subset into a new isolated resource directory."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from compare_catalog_update_assets import parse_inventory


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def materialize(source, inventory, inventory_sha, manifest, manifest_sha, output, min_free):
    if digest(inventory) != inventory_sha or digest(manifest) != manifest_sha:
        raise ValueError("inventory identity mismatch")
    if source.is_symlink() or not source.is_dir() or output.exists() or output.is_symlink():
        raise ValueError("source or destination invalid")
    source = source.resolve(strict=True)
    output_parent = output.parent.resolve(strict=True)
    if output_parent == source or source in output_parent.parents:
        raise ValueError("destination inside source")
    expected = parse_inventory(inventory)
    original = json.loads(manifest.read_text())
    if (set(original) != {"assets", "data", "search", "update"} or
            original["assets"] != original["data"] or original["search"] != original["update"] or
            any(original["update"].get(name) != value for name, value in expected.items())):
        raise ValueError("subset differs from source manifest")
    size = 0
    for name, value in expected.items():
        path = source / name
        if any(component.is_symlink() for component in [path, *path.parents] if component != source.parent):
            raise ValueError("resource symlink rejected")
        if not path.is_file() or digest(path) != value:
            raise ValueError("source resource identity differs")
        size += path.stat().st_size
    if shutil.disk_usage(output_parent).free - size < min_free:
        raise ValueError("insufficient free space for isolated copy")
    output.mkdir(mode=0o700)
    copied = output / "resources"
    copied.mkdir(mode=0o700)
    try:
        for name, value in expected.items():
            destination = copied / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (source / name).open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            if digest(destination) != value or digest(source / name) != value:
                raise ValueError("copy or source identity changed")
        output_manifest = {"assets": original["assets"], "data": original["data"],
                           "search": expected, "update": expected}
        manifest_path = output / "resource-manifest.json"
        with manifest_path.open("x") as stream:
            json.dump(output_manifest, stream, sort_keys=True)
            stream.write("\n")
        result = {"status": "COPIED", "files": len(expected), "bytes": size,
                  "source_inventory_sha256": inventory_sha, "original_manifest_sha256": manifest_sha,
                  "resource_manifest_sha256": digest(manifest_path)}
    except BaseException as error:
        result = {"status": "FAILED", "error": type(error).__name__ + ": " + str(error)}
        with (output / "result.json").open("x") as stream:
            json.dump(result, stream)
            stream.write("\n")
        raise
    with (output / "result.json").open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "inventory", "manifest", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("inventory-sha256", "manifest-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--min-free-bytes", type=int, default=200 * 1024 * 1024)
    args = parser.parse_args()
    if args.min_free_bytes < 0:
        parser.error("min-free-bytes must be nonnegative")
    print(json.dumps(materialize(args.source, args.inventory, args.inventory_sha256, args.manifest,
                                 args.manifest_sha256, args.output, args.min_free_bytes)))


if __name__ == "__main__":
    main()
