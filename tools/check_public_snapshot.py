import gzip
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SUFFIXES = {".pt", ".pth", ".so", ".apk", ".o", ".pem", ".key", ".safetensors"}
SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def main():
    manifest = json.loads((ROOT / "publication/source-manifest.json").read_text())
    seen = set()
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or entry["path"] in seen:
            raise ValueError("invalid or duplicate source path")
        seen.add(entry["path"])
        path = ROOT / relative
        if path.is_symlink():
            raise ValueError("symlink in publication")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"] or len(content) != entry["bytes"]:
            raise ValueError("source manifest mismatch: " + str(relative))
        if path.suffix in FORBIDDEN_SUFFIXES or path.name == "MACHINES.md":
            raise ValueError("private or binary attachment: " + str(relative))
        if path.name.startswith(".env") and not path.name.endswith(".example"):
            raise ValueError("local environment in publication")
        text = (gzip.decompress(content) if path.name.endswith(".json.gz") else content).decode("utf-8-sig")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise ValueError("credential signature: " + str(relative))
    if len(seen) != manifest["file_count"]:
        raise ValueError("source file count mismatch")
    print(json.dumps({"status": "passed", "source_files_verified": len(seen)}))


if __name__ == "__main__":
    main()
