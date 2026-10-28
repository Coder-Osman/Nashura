"""Extract a bounded write failure; this observation never opens the catalog gate."""

import json
import re


def native_queue_observations(stdout, stderr, expected_entries=(12, 12)):
    marker = "PREFERENCE_BACKEND_HOOK\thistorical_ascii_native_backup_candidate_parity_unverified"
    lines = stdout.splitlines()
    hooks = [line for line in lines if line.startswith("PREFERENCE_BACKEND_HOOK")]
    queue_lines = [line for line in lines if line.startswith("PREFERENCE_NATIVE_BACKUP_QUEUE")]
    write_lines = [line for line in lines if line.startswith("PREFERENCE_BACKEND_WRITE")]
    queue = summary = None
    valid = False
    try:
        if len(queue_lines) == len(write_lines) == 1:
            queue = json.loads(queue_lines[0].split("\t", 1)[1])
            summary = json.loads(write_lines[0].split("\t", 1)[1])
            expected_queue = {"result": 0, "created": True, "delivery": "PENDING",
                              "android_parity": "NOT_VERIFIED"}
            expected_write = {"result": 0, "deleted": False, "commits": 2, "disk_writes": 2,
                              "false_commits": 0, "backup_requests": 1, "backup_failures": 0,
                              "decode_exceptions": 0, "entries": list(expected_entries)}
            valid = all(type(actual) is dict and actual.keys() == expected.keys() and
                        all(type(actual[key]) is type(value) and actual[key] == value
                            for key, value in expected.items())
                        for actual, expected in [(queue, expected_queue), (summary, expected_write)])
            valid = valid and all(type(value) is int for value in summary["entries"])
            valid = valid and hooks == [marker] and lines.index(marker) < lines.index(queue_lines[0]) < lines.index(write_lines[0])
            valid = valid and not any(line.startswith(("PREFERENCE_BACKEND_FATAL", "PREFERENCE_BACKUP\t",
                                                       "PREFERENCE_DECODE_EXCEPTION"))
                                      for line in stderr.splitlines())
    except (ValueError, TypeError, IndexError, KeyError):
        valid = False
    return {"candidate_hook_bound": hooks == [marker], "queue_summary": queue,
            "write_summary": summary, "bounded_enqueue_observed": valid,
            "delivery": "PENDING" if valid else "UNKNOWN",
            "android_backup_parity": "NOT_VERIFIED", "current_runtime_identity": "UNRESOLVED",
            "configuration_contract_pass": False, "catalog_ready_pass": False}


def observations(stdout, stderr, expected_entries=(12, 12)):
    marker = "PREFERENCE_BACKEND_HOOK\thistorical_identity_ascii_xml_only_backup_unresolved"
    hooks = [line for line in stdout.splitlines() if line.startswith("PREFERENCE_BACKEND_HOOK")]
    writes = [line for line in stdout.splitlines() if line.startswith("PREFERENCE_BACKEND_WRITE")]
    backups = [line for line in stderr.splitlines() if line.startswith("PREFERENCE_BACKUP")]
    failures = [line for line in stderr.splitlines() if line.startswith("PREFERENCE_BACKEND_FATAL")]
    summary = None
    if len(writes) == 1:
        try:
            prefix, encoded = writes[0].split("\t", 1)
            if prefix == "PREFERENCE_BACKEND_WRITE":
                summary = json.loads(encoded)
        except (ValueError, TypeError):
            pass
    expected = {"result": -1, "deleted": False, "commits": 2, "disk_writes": 2,
                "false_commits": 0, "backup_requests": 1, "backup_failures": 1,
                "decode_exceptions": 0, "entries": list(expected_entries)}
    equal = type(summary) is dict and summary.keys() == expected.keys()
    equal = equal and all(type(summary[key]) is type(value) and summary[key] == value
                          for key, value in expected.items())
    equal = equal and all(type(value) is int for value in summary["entries"])
    bounded = (hooks == [marker] and equal and len(backups) == 1 and
               re.fullmatch(r"PREFERENCE_BACKUP\tUNRESOLVED\tTID\t[1-9][0-9]*", backups[0]) is not None and
               failures == ["PREFERENCE_BACKEND_FATAL\tbackup service unresolved after persistence"])
    return {"hook_bound": hooks == [marker], "write_summary": summary,
            "persistence_before_unresolved_backup_observed": bounded,
            "backup_service": "UNRESOLVED", "current_runtime_identity": "UNRESOLVED",
            "configuration_contract_pass": False, "catalog_ready_pass": False}
