from copy import deepcopy

import pytest

from preference_cipher_job import read_evidence, validate


def fixture():
    failure = {"result": -3, "length": 0, "matches": False, "output_unchanged": True}
    success = {"result": 0, "length": 16, "matches": True, "output_unchanged": False}
    summary = {"status": "PASS", "scope": "primitive_candidate_public_key_real_dex_iv",
               "libg_loaded": False, "known_answer_first_blocks": 2, "boundary_cases": 20,
               "nonaligned": [failure.copy() for _ in range(4)],
               "bad_padding": [failure.copy(), success.copy(), success.copy(), success.copy()]}
    identities = {name: name + "-sha" for name in ["libcrypto.so", "probe", "linker64", "libc.so", "libdl.so"]}
    return summary, [{"name": name, "sha256": checksum} for name, checksum in identities.items()], identities


def test_candidate_gate_requires_real_mapping_and_preserves_nonrecovering_sequence():
    assert validate(*fixture())


@pytest.mark.parametrize("damage", ["null", "count", "scope", "claims_libg", "missing_crypto",
    "crypto_sha", "duplicate_crypto", "mapped_libg", "empty_sequence", "leaked_failure",
    "unexpected_result", "unknown_field", "first_success", "unknown_library", "wrong_libc",
    "missing_linker", "false_match", "invalid_success_length"])
def test_invalid_evidence_rejected(damage):
    summary, mapped, inputs = deepcopy(fixture())
    if damage == "null": summary = None
    elif damage == "count": summary["boundary_cases"] = 0
    elif damage == "scope": summary["scope"] = "android_equivalent"
    elif damage == "claims_libg": summary["libg_loaded"] = True
    elif damage == "missing_crypto": mapped = []
    elif damage == "crypto_sha": mapped[0]["sha256"] = "different"
    elif damage == "duplicate_crypto": mapped *= 2
    elif damage == "mapped_libg": mapped.append({"name": "libg.so", "sha256": "engine"})
    elif damage == "empty_sequence": summary["nonaligned"] = []
    elif damage == "leaked_failure": summary["nonaligned"][0]["length"] = 1
    elif damage == "unexpected_result": summary["nonaligned"][1]["result"] = 1
    elif damage == "unknown_field": summary["nonaligned"][1]["extra"] = True
    elif damage == "first_success": summary["nonaligned"][0]["result"] = 0
    elif damage == "unknown_library": mapped.append({"name": "unknown.so", "sha256": "unknown"})
    elif damage == "wrong_libc": mapped[3]["sha256"] = "wrong"
    elif damage == "missing_linker": mapped.pop(2)
    elif damage == "false_match": summary["bad_padding"][1]["matches"] = False
    elif damage == "invalid_success_length": summary["bad_padding"][1]["length"] = 123
    assert not validate(summary, mapped, inputs)


def test_mapping_read_failure_does_not_erase_valid_assertions(tmp_path):
    import json
    summary, _, _ = fixture()
    (tmp_path / "stdout.log").write_text(json.dumps(summary))
    (tmp_path / "stderr.log").write_text("mapped:/nonexistent/nashura/linker64\nmapped:[vdso]\n")
    parsed, mapped, errors = read_evidence(tmp_path)
    assert parsed == summary
    assert errors == ["mapping:FileNotFoundError"]
    assert mapped == [{"name": "[vdso]", "path": "[vdso]", "sha256": None}]


def test_unresolved_mapping_explicitly_rejected(tmp_path):
    (tmp_path / "stdout.log").write_text("{}")
    (tmp_path / "stderr.log").write_text("unresolved_mapping:linker\nmapped:relative.so\n")
    assert read_evidence(tmp_path)[2] == ["unresolved_mapping", "nonabsolute_mapping"]
