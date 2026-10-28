"""Compare observed lookup keys without publishing the source value."""

import argparse
import hashlib
import json
from pathlib import Path

from audit_finish_lookup import audit as audit_lookup


def compare_key(android, linux):
    left = [row for row in android["trace"]["events"] if row.get("kind") == "finish_lookup"]
    right = [row for row in linux["events"] if row.get("name") == "finish_lookup"]
    if len(left) != 1 or len(right) != 1 or not isinstance(left[0].get("key"), str):
        raise ValueError("lookup key observation missing")
    try:
        android_bytes = left[0]["key"].encode("ascii")
        value = right[0]["strings"]["x1"]
        if value.get("encoding") != "engine-string16" or value.get("terminated") is not True:
            raise ValueError("Linux key encoding incomplete")
        linux_bytes = bytes.fromhex(value["bytes_hex"])
    except (KeyError, TypeError, UnicodeError) as error:
        raise ValueError("lookup key observation malformed") from error
    if (not android_bytes or len(android_bytes) > 1024 or value.get("length") != len(linux_bytes) or
            any(byte < 32 or byte > 126 for byte in android_bytes)):
        raise ValueError("lookup key observation out of bounds")
    return {"android_length": len(android_bytes), "linux_length": len(linux_bytes),
            "same_bytes": android_bytes == linux_bytes,
            "android_sha256": hashlib.sha256(android_bytes).hexdigest(),
            "linux_sha256": hashlib.sha256(linux_bytes).hexdigest()}


def audit(paths, identities):
    baseline = audit_lookup(paths, identities)
    android = json.loads(paths["android"].read_text())
    linux = json.loads(paths["linux_observation"].read_text())
    return {"schema": "nashura.finish-lookup-key.v1", "status": "RUNTIME_OBSERVED_LIMITED",
            "input_sha256": identities, "matched_update_files": baseline["matched_update_files"],
            "lookup": baseline["comparison"], "key": compare_key(android, linux),
            "catalog_ready_pass": False,
            "limitations": ["matching key does not prove object or container equality",
                            "only a single matched lookup per runtime observed",
                            "separate processes and platform runtimes", "configuration and Backup unresolved"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("android", "android_before", "android_after", "trace_script", "linux_result", "linux_observation")
    for name in names:
        option = name.replace("_", "-")
        parser.add_argument("--" + option, type=Path, required=True)
        parser.add_argument("--" + option + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit({name: getattr(args, name) for name in names},
                   {name: getattr(args, name + "_sha256") for name in names})
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
