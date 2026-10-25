import argparse
import gzip
import hashlib
import json
import os
from collections import Counter
from pathlib import Path


TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".toml", ".json", ".xml", ".csv", ".sha256",
    ".c", ".cpp", ".h", ".hpp", ".inc", ".S", ".sh", ".ps1", ".cmd",
    ".command", ".js", ".cjs", ".html", ".css", ".yml", ".yaml", ".svg",
}
EXCLUDED_DIRECTORIES = {
    ".git", ".venv", ".venv-live", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "vendor", "out", "artifacts", "build", "builds",
    "checkpoints", "evaluations", "runs", "training_replays", "user-provided",
    "private-audit", ".audit", ".runtime", "wandb",
}
SPECIAL_NAMES = {"LICENSE", "NOTICE", ".gitignore", ".gitattributes", ".clang-format"}
COMPILED_TABLES = {"card_specs.json.gz", "card_logic.json.gz", "effects.json.gz",
    "projectiles.json.gz", "model_catalogs.json.gz"}
EVIDENCE_ROOT = Path("_spec_work/evidence/fair-final-v1-20261019")
NORMALIZED_EOF_PATHS = {
    Path("_spec_work/versions/fair-final-affordability-boundaries-v1") / name
    for name in ("cases.py", "probe.py", "run_native.py", "verify.py")
}


def digest(content):
    return hashlib.sha256(content).hexdigest()


def local_secrets(workspace):
    secrets = set()
    machines = workspace / "MACHINES.md"
    if machines.exists():
        for line in machines.read_text().splitlines():
            fields = [field.strip() for field in line.split("|")]
            if len(fields) > 4 and fields[1].startswith("172."):
                if len(fields[4]) >= 6:
                    secrets.add(fields[4])
    environment = workspace / "firstlight_CR/.env.render-web"
    if environment.exists():
        for line in environment.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                name, value = line.split("=", 1)
                value = value.strip().strip("\"'")
                if any(word in name.lower() for word in ("password", "token", "secret", "key")) and len(value) >= 6:
                    secrets.add(value)
    return secrets


def source_files(root):
    for current, directories, files in os.walk(root):
        directories[:] = sorted(directory for directory in directories
            if directory not in EXCLUDED_DIRECTORIES and not directory.startswith(".venv")
            and not (Path(current) / directory).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if not path.is_symlink():
                yield path


def selected_files(workspace):
    yield from source_files(workspace / "firstlight_CR")
    versions = workspace / "_spec_work/versions"
    for version in sorted(versions.iterdir()):
        if version.is_dir():
            yield from sorted(path for path in version.iterdir() if path.is_file())
    yield from source_files(versions / "fair-final-history-v2/project")
    evidence = workspace / EVIDENCE_ROOT
    for path in sorted(evidence.iterdir()):
        if path.is_file() and (path.suffix in {".md", ".py", ".xml"} or path.name == "gate-status.json"):
            yield path
    yield from source_files(evidence / "spec")
    native = evidence / "phase-snapshot-engine-1792485763481586000"
    yield from source_files(native)
    frozen = workspace / "firstlight_CR/artifacts/standalone-resident-20260927"
    for path in source_files(frozen):
        if path.suffix in {".c", ".cpp", ".h", ".inc", ".S"}:
            yield path
    compact = workspace / ".tmp_remote_edit/compact_patch"
    for name in ("resident.cpp", "resident_multimatch.inc"):
        if (compact / name).exists():
            yield compact / name
    for name in (
        "value-direct-20261012", "new-value-teacher-20261012", "new-value-policy-screen-20261013",
        "teacher-sampling-eval-20261013", "distillation-document-p0-20261013",
        "consumer-comparison-20261013", "r2-postround-20261014", "continuous-training-20261014",
        "teacher-r0-vs-m0-20261014", "fair-search-20261010",
    ):
        readme = workspace / "_spec_work/evidence" / name / "README.md"
        if readme.exists():
            yield readme
    for name in ("TRAINING_ENGINE_PLAN.md", "wire-spec.md"):
        if (workspace / name).exists():
            yield workspace / name


def export(workspace, destination):
    secrets = local_secrets(workspace)
    entries = []
    exclusions = Counter()
    for path in sorted(set(selected_files(workspace))):
        relative = path.relative_to(workspace)
        if path.name.startswith(".env") and not path.name.endswith(".example"):
            exclusions["local_environment"] += 1
            continue
        compiled_table = path.name in COMPILED_TABLES and path.parent.parts[-2:] == ("data", "competitive")
        if (path.suffix not in TEXT_SUFFIXES and path.name not in SPECIAL_NAMES
                and not path.name.endswith(".example") and not compiled_table):
            exclusions["binary_or_unselected_format"] += 1
            continue
        if path.stat().st_size > 2 * 1024**2:
            exclusions["oversized_generated_file"] += 1
            continue
        content = path.read_bytes()
        original_sha = digest(content)
        try:
            text = (gzip.decompress(content) if compiled_table else content).decode("utf-8-sig")
        except UnicodeDecodeError:
            exclusions["non_text"] += 1
            continue
        transformations = []
        for secret in secrets:
            if secret in text:
                if path.name != "demo_match_set.py":
                    raise ValueError("local credential found in selected file: " + str(relative))
                text = text.replace('"' + secret + '"', '""')
                if secret in text:
                    raise ValueError("credential requires manual review: " + str(relative))
                transformations.append("removed embedded credential default; use CR_REMOTE_PASSWORD")
        if relative in NORMALIZED_EOF_PATHS and text.endswith("\n\n"):
            text = text.rstrip("\n") + "\n"
            transformations.append("removed extra EOF blank lines in public copy; frozen native source hashes preserved")
        if transformations:
            content = text.encode("utf-8")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_bytes() != content:
            target.write_bytes(content)
        entries.append({"path": relative.as_posix(), "sha256": digest(content), "bytes": len(content),
            "source_sha256": original_sha, "transformations": transformations})
    manifest = {
        "schema": "hashura.public-source-snapshot.v1",
        "files": entries,
        "file_count": len(entries),
        "source_bytes": sum(entry["bytes"] for entry in entries),
        "excluded_selected_candidates": dict(exclusions),
        "excluded_categories": ["credentials", "private game audits", "raw episodes", "model weights",
            "third-party game binaries and resources", "generated native binaries", "runtime environments"],
        "evidence_note": "Original evidence manifests retain original hashes; omitted external artifacts are not bundled.",
    }
    manifest_path = destination / "publication/source-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"files": manifest["file_count"], "bytes": manifest["source_bytes"],
        "exclusions": dict(exclusions)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    arguments = parser.parse_args()
    workspace = arguments.workspace.resolve()
    destination = arguments.destination.resolve()
    if workspace == destination:
        raise ValueError("publication must use a separate directory")
    export(workspace, destination)


if __name__ == "__main__":
    main()
