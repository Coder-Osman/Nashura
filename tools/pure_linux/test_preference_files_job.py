from copy import deepcopy

import pytest

from preference_files_job import CASES, validate


def fixture():
    summary = {"status": "PASS", "scope": "synthetic_file_commit_and_process_crash_only",
               "libg_loaded": False, "cases": list(CASES), "case_count": len(CASES), "assertions": 402}
    identities = {name: name + "-sha" for name in ["probe", "linker64", "libc.so", "libdl.so"]}
    mapped = [{"name": name, "sha256": checksum, "path": "/isolated/" + name}
              for name, checksum in identities.items()]
    return summary, mapped, identities


def test_bounded_files_evidence_accepted():
    assert validate(*fixture())


@pytest.mark.parametrize("damage", ["null", "cases", "order", "count", "bool_count", "assertions",
    "scope", "libg", "extra", "mapping_null", "mapping_row", "missing_libc", "sha", "relative",
    "duplicate", "libcrypto", "libart", "unknown", "vdso"])
def test_damaged_evidence_rejected(damage):
    summary, mapped, identities = deepcopy(fixture())
    if damage == "null": summary = None
    elif damage == "cases": summary["cases"].pop()
    elif damage == "order": summary["cases"].reverse()
    elif damage == "count": summary["case_count"] = 0
    elif damage == "bool_count": summary["case_count"] = True
    elif damage == "assertions": summary["assertions"] = True
    elif damage == "scope": summary["scope"] = "android_equivalent"
    elif damage == "libg": summary["libg_loaded"] = True
    elif damage == "extra": summary["ready"] = True
    elif damage == "mapping_null": mapped = None
    elif damage == "mapping_row": mapped.append(None)
    elif damage == "missing_libc": mapped.pop(2)
    elif damage == "sha": mapped[1]["sha256"] = "wrong"
    elif damage == "relative": mapped[0]["path"] = "relative"
    elif damage == "duplicate": mapped *= 2
    elif damage in {"libcrypto", "libart", "unknown"}:
        name = damage + ".so"
        mapped.append({"name": name, "sha256": "extra", "path": "/" + name})
        identities[name] = "extra"
        if damage == "unknown": identities.pop(name)
    elif damage == "vdso": mapped.append({"name": "[vdso]", "path": "[vdso]", "sha256": "invented"})
    assert not validate(summary, mapped, identities)
