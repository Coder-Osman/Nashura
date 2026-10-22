import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from build_payload import build, SERIES, SELECTION, PROBE
from verify_execution import implementation_identity


def test_runtime_is_strict_and_repeatable_in_fresh_process():
    script = '''import json,torch
from deterministic_runtime import configure
first=configure()
second=configure()
assert first==second
assert first['deterministic'] and not first['warn_only']
assert first['cudnn_deterministic'] and not first['cudnn_benchmark']
assert not first['matmul_allow_tf32'] and not first['cudnn_allow_tf32']
assert first['cublas_workspace_config']==':4096:8'
print(json.dumps(first))
'''
    result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
        capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout)["deterministic"] is True


def test_runtime_rejects_late_cuda_workspace_change(monkeypatch):
    import deterministic_runtime
    monkeypatch.setattr(deterministic_runtime.torch.cuda, "is_initialized", lambda: True)
    monkeypatch.setenv("CUBLAS_WORKSPACE_CONFIG", ":16:8")
    with pytest.raises(RuntimeError, match="before CUDA initialization"):
        deterministic_runtime.configure()


def test_payload_binds_runtime_and_both_process_entrypoints(tmp_path):
    parent = ROOT.parent / "fair-final-migration-integration-v1/native-selected-round0-1792629813912401000"
    package, identity, _ = build(parent, deterministic=True)
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    for name, expected in identity.items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == expected
    for name in (SELECTION + "train_round.py", PROBE):
        source = (tmp_path / name).read_text()
        assert source.index("print(json.dumps(configure())") < source.index("teacher, _, _ = load_checkpoint")
    before = implementation_identity(tmp_path)
    runtime = tmp_path / SERIES / "deterministic_runtime.py"
    runtime.write_bytes(runtime.read_bytes() + b"\n")
    assert implementation_identity(tmp_path) != before
    _, legacy_identity, _ = build(parent)
    assert SERIES + "deterministic_runtime.py" not in legacy_identity
    assert legacy_identity[SELECTION + "train_round.py"] != identity[SELECTION + "train_round.py"]
