import argparse
from pathlib import Path
import sys

def validate_continuation(attempt, *, source=None):
    from continuation_evidence import audit_checkpoint_prefix, audit_failed_prefix, read, sha
    from verify_search_episode import SOURCE, validate_episode
    if source is None:
        source = SOURCE
    attempt = Path(attempt)
    launch = read(attempt / "launch.json")
    parent = Path(launch["parent"])
    parent_completion = read(parent / "evidence/completion.json")
    if parent_completion.get("checkpointed") is True:
        prefix = audit_checkpoint_prefix(parent, source=source)
    else:
        prefix = audit_failed_prefix(parent, source=source)
    directory = attempt / "evidence"
    receipt = read(directory / "continuation-input.json")
    if (receipt != prefix or sha(directory / "continuation-input.json") != launch["input_sha256"]
            or sha(parent / "launch.json") != launch["parent_launch_sha256"]):
        raise ValueError("continuation parent or input receipt differs")
    for relative, expected in launch["payload_sha256"].items():
        if sha(directory / relative) != expected:
            raise ValueError("continuation payload differs: " + relative)
    if (type(launch["settings"]["wall_seconds"]) is not int
            or launch["settings"]["wall_seconds"] not in (21600, 86400)
            or launch["settings"]["segment_reserve_seconds"] != 1800
            or launch["settings"]["training_admissible"] is not False
            or launch["source_sha256"] != receipt["source_sha256"]):
        raise ValueError("continuation protocol or phase bound differs")
    recovery = read(directory / "episode/recovery-continuation-report.json")
    expected = dict(status="passed_replay_only", training_admissible=False, gates_closed=False,
        input_sha256=launch["input_sha256"], manifest_sha=receipt["manifest_sha"],
        record_sha=receipt["record_sha"], records=receipt["records"], proof=receipt["proof"],
        committed_groups=receipt["committed_groups"], episode_id=receipt["episode_id"])
    if recovery != expected:
        raise ValueError("continuation native replay witness differs")
    original = parent / "evidence/episode/private-audit"
    current = directory / "episode/private-audit"
    for path in sorted(original.glob("[0-9]*.json")):
        if sha(current / path.name) != sha(path):
            raise ValueError("continuation rewrote the parent journal prefix")
    if sha(current / "manifest.json") != receipt["manifest_sha"]:
        raise ValueError("continuation logical episode changed")
    for name, expected_sha in receipt["boundary_sha256"].items():
        if sha(directory / "episode/search-boundaries" / name) != expected_sha:
            raise ValueError("continuation rewrote parent search evidence")
    report = validate_episode(directory, launch=launch, source=source, expected_phase_names=("continue",))
    report.update(scope="complete Full128 engineering continuation with verified parent prefix",
        parent_attempt=launch["parent"], origin_failed_attempt=prefix.get(
            "origin_failed_attempt", prefix["parent_attempt"]),
        continued_from_tick=receipt["proof"]["tick"],
        parent_prefix_roots=receipt["roots"], parent_prefix_records=receipt["records"],
        parent_failure_preserved=prefix["status"] == "verified_failed_prefix",
        parent_checkpoint_preserved=prefix["status"] == "verified_checkpoint_prefix")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("attempt", type=Path)
    parser.add_argument("--source-root", type=Path,
        default=Path(__file__).resolve().parents[3] / "firstlight_CR")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    from native_runner.training.fair_final.episode import atomic_json
    report = validate_continuation(args.attempt, source=args.source_root)
    atomic_json(args.attempt / "verification.json", report)
    print(report)


if __name__ == "__main__":
    main()
