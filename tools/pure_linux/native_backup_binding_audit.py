"""Audit a bounded libg backup candidate batch; never grant catalog parity."""

import argparse
import hashlib
import json
from pathlib import Path

from parsed_tables import observations as table_observations
from preference_hook_evidence import native_queue_observations, observations


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(root, expected_entries=(12, 12)):
    batch = json.loads((root / "batch-result.json").read_text())
    require(batch["machine"] == "aarch64" and len(batch["records"]) == 3, "batch shape")
    reports = []
    for ordinal, record in enumerate(batch["records"]):
        path = root / f"process-{ordinal}"
        result = json.loads((path / "result.json").read_text())
        require(record["exit_code"] == 1 and result["status"] == "BLOCKED", "strict wrapper gate")
        require(result["catalog_ready_pass"] is False, "strict catalog gate")
        require(result["exit_code"] == (0 if ordinal else 96) and result["timed_out"] is False,
                "native exit")
        require(result["probe_sha256"] == batch["probe_sha256"], "probe identity")
        for field in ("inputs_unchanged", "probe_unchanged", "resources_unchanged", "auxiliary_unchanged"):
            require(result[field] is True, field)
        require(result["postcheck_errors"] == [], "postcheck errors")
        require(result["inputs_before"] == result["inputs_after"], "input identities")
        output_hashes = {}
        for name, expected in result["outputs"].items():
            require(Path(name).name == name and not (path / name).is_symlink(), "output name")
            actual = digest(path / name)
            require(actual == expected, "output hash: " + name)
            output_hashes[name] = actual
        stdout = (path / "stdout.log").read_text()
        stderr = (path / "stderr.log").read_text()
        report = {"ordinal": ordinal, "native_exit": result["exit_code"],
                  "wrapper_exit": record["exit_code"], "elapsed_seconds": result["elapsed_seconds"],
                  "result_sha256": digest(path / "result.json"), "outputs": output_hashes,
                  "inputs_unchanged": True, "postcheck_errors": []}
        if not ordinal:
            control = observations(stdout, stderr, expected_entries)
            require(control["persistence_before_unresolved_backup_observed"] is True, "control observation")
            report["configuration"] = control
        else:
            candidate = native_queue_observations(stdout, stderr, expected_entries)
            tables = table_observations(stdout)
            require(candidate["bounded_enqueue_observed"] is True, "candidate observation")
            require(tables["complete"] is True and tables["summaries"]["after"] == [162, 24353], "table observation")
            for marker in ("LOADER_CANARY\t1", "CATALOG_READY_AFTER_LOADER\t1",
                           "NATIVE_ASSET_FINISH_RESULT\t0", "PHASE\tafter_close"):
                require(stdout.splitlines().count(marker) == 1, "lifecycle marker: " + marker)
            require(result["catalog"]["loader_destroyed"] is True, "loader destructor")
            require(record["consumer_exit"] == 0, "consumer exit")
            receipt = json.loads((path / "consumer.json").read_text())
            require(receipt["status"] == "NATIVE_ARCHIVE_CONSUMED" and
                    receipt["android_backup_parity"] == "NOT_VERIFIED", "receipt status")
            store = path / "preference-store"
            archives = list(store.glob("backup-*"))
            require(len(archives) == 1 and not archives[0].is_symlink(), "archive count")
            manifest = json.loads((archives[0] / "manifest.json").read_text())
            require({key: value for key, value in receipt.items() if key != "status"} == manifest,
                    "archive receipt")
            require(not (store / ".backup-pending").exists(), "pending not acknowledged")
            require(set(receipt["files"]) == {"store-0.xml", "store-1.xml"}, "receipt slots")
            for name, identity in receipt["files"].items():
                require((store / name).read_bytes() == (archives[0] / name).read_bytes(), "archive bytes")
                require(digest(store / name) == identity["sha256"] and
                        (store / name).stat().st_size == identity["bytes"], "slot identity")
            report.update(configuration=candidate, parsed_tables=tables,
                          consumer_exit=0, consumer_sha256=digest(path / "consumer.json"),
                          archive_files=receipt["files"], pending_acknowledged=True)
        reports.append(report)
    require(reports[1]["parsed_tables"] == reports[2]["parsed_tables"], "repeat table layout")
    require(reports[1]["archive_files"] == reports[2]["archive_files"], "repeat slot bytes")
    return {"schema": "nashura.native-backup-binding-audit.v1", "audit": "PASS",
            "scope": "bounded candidate enqueue, natural loader and native archive only",
            "machine": batch["machine"], "kernel": batch["kernel"],
            "batch_sha256": digest(root / "batch-result.json"), "probe_sha256": batch["probe_sha256"],
            "reports": reports, "android_backup_parity": "NOT_VERIFIED",
            "current_runtime_identity": "UNRESOLVED", "configuration_contract_pass": False,
            "catalog_ready_pass": False, "resource_manager": "NOT_RUN", "gamestate": "NOT_RUN"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = audit(arguments.experiment)
    with arguments.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
