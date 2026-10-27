"""Extract a bounded write failure; this observation never opens the catalog gate."""

import json
import re


def observations(stdout, stderr):
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
                "decode_exceptions": 0, "entries": [12, 12]}
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
