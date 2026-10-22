"""Batch preparation preserves identity checks and skips redundant transfers."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from native_runner.training import event_history_assets as assets
from native_runner.training.event_opponents import QUOTAS
from tools.event_rl_persistent_fleet import PersistentFleet

CONTRACT = "e" * 64


def fixture_asset(root, label):
    (root / "weights").mkdir(parents=True, exist_ok=True)
    directory = root / "history-inference"
    directory.mkdir(exist_ok=True)
    source = ("complete-checkpoint:" + label).encode()
    sha = hashlib.sha256(source).hexdigest()
    (root / "weights" / (sha + ".pt")).write_bytes(source)
    content = ("inference:" + label).encode()
    artifact = hashlib.sha256(content).hexdigest()
    path = directory / (artifact + ".pt")
    path.write_bytes(content)
    descriptor = dict(path=str(path), source_checkpoint_sha256=sha,
        artifact_sha256=artifact, contract_sha256=CONTRACT, bytes=len(content), source_bytes=len(source))
    (directory / (sha + ".json")).write_text(json.dumps(descriptor))
    return sha, descriptor


def test_cache_hit_does_not_import_torch_and_hashes_both_files(tmp_path):
    sha, d = fixture_asset(tmp_path, "600")
    script = """import json,sys
from native_runner.training.event_history_assets import archive_descriptors
r=archive_descriptors(sys.argv[1],[sys.argv[2]],sys.argv[3])
assert 'torch' not in sys.modules
print(json.dumps(r))
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), sha, CONTRACT],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["descriptors"][sha] == d
    (tmp_path / "weights" / (sha + ".pt")).write_bytes(b"broken")
    with pytest.raises(ValueError, match="checkpoint SHA"):
        assets.archive_descriptors(tmp_path, [sha], CONTRACT)


def test_missing_export_is_cached_and_contract_failure_is_not_published(tmp_path, monkeypatch):
    sha, d = fixture_asset(tmp_path, "600")
    index = tmp_path / "history-inference" / (sha + ".json")
    index.unlink()
    calls = []
    def export(source, directory):
        calls.append(str(source))
        return d
    monkeypatch.setitem(sys.modules, "native_runner.training.event_inference", SimpleNamespace(export_inference=export))
    assert assets.archive_descriptors(tmp_path, [sha], CONTRACT)["exported"] == 1
    assert assets.archive_descriptors(tmp_path, [sha], CONTRACT)["exported"] == 0
    assert len(calls) == 1
    index.unlink()
    with pytest.raises(ValueError, match="contract"):
        assets.archive_descriptors(tmp_path, [sha], "f" * 64)
    assert not index.exists()


@pytest.mark.parametrize("damage", ["artifact", "contract", "path", "size", "missing_source"])
def test_invalid_archive_never_silently_falls_back(tmp_path, damage):
    sha, d = fixture_asset(tmp_path, "645")
    index = tmp_path / "history-inference" / (sha + ".json")
    if damage == "artifact":
        Path(d["path"]).write_bytes(b"x" * d["bytes"])
    elif damage == "missing_source":
        (tmp_path / "weights" / (sha + ".pt")).unlink()
    else:
        key = {"contract": "contract_sha256", "path": "path", "size": "bytes"}[damage]
        d[key] = {"contract": "a"*64, "path": str(tmp_path / "elsewhere.pt"), "size": d["bytes"]+1}[damage]
        index.write_text(json.dumps(d))
    with pytest.raises((ValueError, FileNotFoundError)):
        assets.archive_descriptors(tmp_path, [sha], CONTRACT)


def test_probe_missing_corrupt_complete_and_disk_guards(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    sha, d = fixture_asset(archive, "650")
    host = tmp_path / "host";host.mkdir()
    result = assets.probe_artifacts(host, {sha:d}, CONTRACT)
    assert result["missing"] == [sha] and result["verified"] == 0
    with pytest.raises(ValueError, match="missing after transfer"):
        assets.probe_artifacts(host, {sha:d}, CONTRACT, require_complete=True)
    target = assets.artifact_path(host, d);shutil.copyfile(d["path"], target)
    assert assets.probe_artifacts(host, {sha:d}, CONTRACT)["missing"] == []
    target.write_bytes(b"x" * d["bytes"])
    with pytest.raises(ValueError, match="SHA"):
        assets.probe_artifacts(host, {sha:d}, CONTRACT)
    shutil.copyfile(d["path"], target)
    monkeypatch.setattr(assets.shutil, "disk_usage", lambda _: SimpleNamespace(free=assets.RESERVE_BYTES+9))
    with pytest.raises(RuntimeError, match="disk guard"):
        assets.probe_artifacts(host, {sha:d}, CONTRACT, current_bytes=10)


class Fleet:
    def __init__(self, root):
        self.ROOT = root / "34"
        self.FLEET = {h: dict(run=str(root/h), ports=list(range(s+n))) for h,(s,n) in QUOTAS.items()}
        for entry in self.FLEET.values():Path(entry["run"]).mkdir(parents=True)
        self.CFG = dict(mixed_opponents={"version":"mixed-history.v1"}, seed_base=2180918000,
                        collector_code="code")
        self.events = []
    def event(self, event, **fields):
        self.events.append(dict(event=event, **fields))


def prepared_fleet(tmp_path, monkeypatch):
    fleet = Fleet(tmp_path)
    descriptor_by_sha = {}
    for i in (600,645,650,675,700,725):
        sha, descriptor = fixture_asset(Path(fleet.FLEET["37"]["run"]), str(i))
        descriptor_by_sha[sha] = descriptor
        directory = fleet.ROOT / f"iteration-{i:06d}";directory.mkdir()
        (directory/"success.json").write_text(json.dumps(dict(iteration=i,checkpoint=str(fleet.ROOT/"weights"/(sha+".pt")))))
    current = sha
    collector = PersistentFleet(fleet)
    collector.descriptor = dict(descriptor_by_sha[current])
    calls=[];transfers=[]
    def request(h, task):
        task=dict(task);operation=task.pop("operation");calls.append((h,operation))
        return assets.archive_descriptors(**task) if operation=="archive" else assets.probe_artifacts(**task)
    def transfer(h, descriptors, missing, *, pull=False):
        transfers.append((h,tuple(missing),pull))
        for sha in missing:
            source=Path(fleet.FLEET[h]["run"]) if pull else fleet.ROOT
            destination=fleet.ROOT if pull else Path(fleet.FLEET[h]["run"])
            shutil.copyfile(assets.artifact_path(source,descriptors[sha]), assets.artifact_path(destination,descriptors[sha]))
    monkeypatch.setattr(collector,"history_request",request)
    monkeypatch.setattr(collector,"transfer_histories",transfer)
    return fleet,collector,fleet.ROOT/"weights"/(current+".pt"),calls,transfers


def test_warm_round_uses_five_requests_zero_transfers_and_preserves_plan(tmp_path, monkeypatch):
    f,c,weight,calls,transfers=prepared_fleet(tmp_path,monkeypatch)
    c.prepare_opponents(weight,iteration=726)
    plan=c.plan
    assert len(c.histories)==5 and len(transfers)==4  # archive pull + three hosts; .37 already has all files
    calls.clear();transfers.clear()
    barrier=threading.Barrier(4)
    original=c.history_request
    def concurrent_request(h, task):
        if task["operation"]=="probe":barrier.wait(timeout=5)
        return original(h,task)
    monkeypatch.setattr(c,"history_request",concurrent_request)
    c.prepare_opponents(weight,iteration=726)
    assert c.plan==plan and not transfers
    assert Counter(operation for _,operation in calls)=={"archive":1,"probe":4}
    assert f.events[-1]["history_preparation"]["archive_exported"]==0
    assert not c.processes  # No collector starts before the preparation barrier.


def test_host_corruption_or_disconnect_blocks_collection(tmp_path, monkeypatch):
    f,c,weight,calls,transfers=prepared_fleet(tmp_path,monkeypatch)
    c.prepare_opponents(weight,iteration=726)
    path=assets.artifact_path(Path(f.FLEET["39"]["run"]),next(iter(c.histories.values())))
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError,match="SHA"):
        c.prepare_opponents(weight,iteration=726)
    assert not c.processes
    assert len(f.events)==1
    monkeypatch.setattr(c,"history_request",lambda *a,**k: (_ for _ in ()).throw(ConnectionError("offline")))
    with pytest.raises(ConnectionError,match="offline"):
        c.prepare_opponents(weight,iteration=726)
    assert len(f.events)==1


def test_transfer_batches_only_missing_files(tmp_path, monkeypatch):
    f,c,weight,calls,transfers=prepared_fleet(tmp_path,monkeypatch)
    sha,d=fixture_asset(tmp_path/"extra","new")
    captured=[]
    f.checked=lambda argv,**kw:captured.append((argv,kw))
    PersistentFleet.transfer_histories(c,"35",{sha:d},[sha])
    assert len(captured)==1
    argv,kw=captured[0]
    assert argv[:4]==["rsync","-a","--ignore-existing","--files-from=-"]
    assert kw["input"]==d["artifact_sha256"]+".pt\n"
    PersistentFleet.transfer_histories(c,"35",{sha:d},[])
    assert len(captured)==1
