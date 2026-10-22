import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[2] / "firstlight_CR"
sys.path.insert(0, str(SOURCE))

from native_runner.training.fair_final.episode import atomic_json, validate_attempt


def inspect_archive(match_id):
    candidates = sorted(ROOT.glob(f"episode-attempt-{match_id}-1-*.tar.gz"))
    if not candidates:
        raise ValueError(f"missing native evidence: {match_id}")
    archive = candidates[-1]
    process_path = archive.with_name(archive.name.removesuffix(".tar.gz") + "-process.json")
    process = json.loads(process_path.read_text())
    if process.get("phase") != "stopped":
        raise ValueError("probe engine cleanup is unconfirmed")
    with tempfile.TemporaryDirectory(prefix="fair-final-verify-") as temporary:
        with tarfile.open(archive) as bundle:
            bundle.extractall(temporary, filter="data")
        directory = Path(temporary) / "attempt"
        report = json.loads((directory / "report.json").read_text())
        if report["status"] != "passed" or report["training_admissible"] is not False:
            raise ValueError("invalid complete-episode acceptance report")
        for relative, expected in report["source_sha256"].items():
            if hashlib.sha256((SOURCE / relative).read_bytes()).hexdigest() != expected:
                raise ValueError(f"native episode evidence is stale: {relative}")
        receipt = report["publication"]["receipt"]
        if hashlib.sha256((directory / "episode.json").read_bytes()).hexdigest() != receipt["episode_sha256"]:
            raise ValueError("completion receipt checksum differs")
        episode = validate_attempt(directory)
        raw = json.loads((Path(temporary) / "audit/native-terminal.json").read_text())
        if any(raw[key] != value for key, value in episode["result"]["native_evidence"].items()):
            raise ValueError("public terminal result differs from isolated native audit")
        counts = {str(owner): sum(row["owner"] == owner for row in episode["rows"]) for owner in (0, 1)}
        last_tick = max(row["tick"] for row in episode["rows"])
        if last_tick >= 6000 or report["rows"] != len(episode["rows"]):
            raise ValueError("decision outside gameplay or incomplete row count")
        return dict(match_id=match_id, archive=archive.name, archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            process_artifact=process_path.name, process_phase=process["phase"],
            rows=len(episode["rows"]), rows_by_owner=counts, terminal=report["terminal"],
            native_result=episode["result"]["native_evidence"], lifecycle=report["lifecycle"],
            seconds=report["seconds"], timing=report["timing"], last_decision_tick=last_tick,
            archive_bytes=archive.stat().st_size, source_sha256=report["source_sha256"],
            training_admissible=False)


def main():
    results = [inspect_archive("p1-complete-native-current"), inspect_archive("p1-native-draw")]
    if results[0]["terminal"]["winner"] not in (0, 1) or results[1]["terminal"]["winner"] is not None:
        raise ValueError("complete win and native draw are both required")
    summary = dict(schema="fair-final-complete-episode-evidence.v1", status="passed",
        scope="two isolated native complete episodes; no search, migration or training admission", cases=results)
    atomic_json(ROOT / "complete-episode-report.json", summary)
    print(json.dumps({"status": "passed", "cases": [{key: result[key]
        for key in ("match_id", "rows", "seconds", "terminal", "last_decision_tick")} for result in results]}, indent=2))


if __name__ == "__main__":
    main()
