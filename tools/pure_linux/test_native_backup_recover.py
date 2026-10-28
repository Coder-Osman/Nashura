import fcntl
import os

import pytest

import native_backup_recover as recovery


@pytest.fixture
def spool(tmp_path):
    tmp_path.chmod(0o700)
    for name in (".preference.lock", ".backup.lock"):
        (tmp_path / name).touch(mode=0o600)
    pending = tmp_path / ".backup-pending"
    pending.write_bytes(b"nashura.native-backup.request.v1\n" + b"a" * 32 + b"\n")
    pending.chmod(0o600)
    os.link(pending, tmp_path / ".backup-request.tmp")
    return tmp_path


def test_recover_same_hardlink_keeps_pending(spool):
    pending = spool / ".backup-pending"
    before = pending.read_bytes()
    result = recovery.recover(spool)
    assert result["request_id"] == "a" * 32 and result["catalog_ready_pass"] is False
    assert pending.read_bytes() == before and pending.stat().st_nlink == 1
    assert not (spool / ".backup-request.tmp").exists()


@pytest.mark.parametrize("damage", ["different_inode", "malformed", "third_link", "mode", "locked", "symlink"])
def test_recovery_preserves_unknown_state(spool, damage):
    pending, staged = spool / ".backup-pending", spool / ".backup-request.tmp"
    if damage == "different_inode":
        staged.unlink()
        staged.write_bytes(pending.read_bytes())
        staged.chmod(0o600)
    elif damage == "malformed":
        pending.write_bytes(b"invalid")
    elif damage == "third_link":
        os.link(pending, spool / "third")
    elif damage == "mode":
        pending.chmod(0o644)
    elif damage == "symlink":
        staged.unlink()
        staged.symlink_to(pending)
    lock = None
    if damage == "locked":
        lock = (spool / ".backup.lock").open("r+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises((ValueError, OSError)):
            recovery.recover(spool)
        assert pending.exists()
        assert staged.is_symlink() if damage == "symlink" else staged.exists()
    finally:
        if lock:
            lock.close()
