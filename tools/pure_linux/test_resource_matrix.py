import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_resource_matrix as matrix


def test_stages_preserve_conflict_and_removed_input_separately(tmp_path):
    present = matrix.create_stage(tmp_path / "present", False)
    removed = matrix.create_stage(tmp_path / "removed", True)
    assert (present[0] / "matrix/conflict.txt").read_bytes() == matrix.APK_CONFLICT
    assert (present[1] / "matrix/conflict.txt").read_bytes() == matrix.UPDATE_CONFLICT
    assert (removed[0] / "matrix/conflict.txt").read_bytes() == matrix.APK_CONFLICT
    assert not (removed[1] / "matrix/conflict.txt").exists()


@pytest.mark.parametrize("missing_close,native_exit,expected", [(False, 60, True), (True, 60, False),
                                                               (False, 95, False)])
def test_invalid_file_requires_missing_and_closed_wrapper(tmp_path, monkeypatch, missing_close, native_exit, expected):
    stage = matrix.create_stage(tmp_path / "present", False)
    backend = tmp_path / "backend"
    backend.write_bytes(b"synthetic backend")

    def fake_run(command, **kwargs):
        output = Path(command[command.index("--output") + 1])
        output.mkdir()
        lines = ["PHASE\tresource_open_return", "RESOURCE_FILE_KIND\tapk", "RESOURCE_INVALID_FILE\t1",
                 "RESOURCE_MISSING\t1", "PROVIDER_SLOTS_RELEASED\t1", "PHASE\tafter_close",
                 "NATIVE_ASSET_FINISH_RESULT\t0"]
        if not missing_close:
            lines.append("PHASE\tresource_close_return")
        (output / "stdout.log").write_text("\n".join(lines))
        (output / "stderr.log").write_text("")
        (output / "result.json").write_text(json.dumps({"inputs_unchanged": True, "resources_unchanged": True,
            "auxiliary_unchanged": True, "probe_unchanged": True, "postcheck_errors": [],
            "timed_out": False, "exit_code": native_exit}))
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(matrix.subprocess, "run", fake_run)
    case = ("present", "data:/matrix/update-only.txt", "missing", None)
    report = matrix.run_case(case, 0, tmp_path, stage, tmp_path, tmp_path / "probe", backend)
    assert report["passed"] is expected
    assert report["cleanup_scope"] == "provider_and_asset_finish"
