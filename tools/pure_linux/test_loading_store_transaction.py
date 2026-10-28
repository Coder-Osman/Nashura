import pytest

from run_loading_store_once import store_and_restore


KEY = "LOADING_SCREEN_STATE_LABEL"


class Exports:
    def __init__(self, original="original", failure=None):
        self.value = original
        self.failure = failure
        self.writes = []

    def readkey(self, key):
        if self.failure == "read_target" and len(self.writes) == 1:
            return {"key": key, "value": "unexpected"}
        return {"key": key, "value": self.value}

    def triggerstore(self, key, value):
        self.writes.append((key, value))
        self.value = value
        if self.failure == "target" and len(self.writes) == 1:
            raise RuntimeError("RPC failed after mutation")
        if self.failure == "restore" and len(self.writes) == 2:
            raise RuntimeError("restore RPC failed")
        return {"invoked": True, "key": key, "value": value}


def run(exports, restore=None, snapshot=None, wait=None):
    records = []
    store_and_restore(exports, KEY, "test", restore, records.append,
                      snapshot or (lambda label: None), wait or (lambda seconds: None))
    return records


def test_restore_captured_original_and_check_both_readbacks():
    exports = Exports()
    records = run(exports)
    assert exports.writes == [(KEY, "test"), (KEY, "original")]
    assert exports.value == "original"
    assert [item["phase"] for item in records][-1] == "read_after_restore"


@pytest.mark.parametrize("failure", ["target", "read_target"])
def test_restore_even_when_mutating_rpc_or_readback_fails(failure):
    exports = Exports(failure=failure)
    with pytest.raises(RuntimeError):
        run(exports)
    assert exports.writes[-1] == (KEY, "original")
    assert exports.value == "original"


@pytest.mark.parametrize("failure", ["snapshot", "interrupt"])
def test_restore_after_snapshot_failure_or_keyboard_interrupt(failure):
    exports = Exports()

    def snapshot(label):
        if label == "after_target_store":
            raise OSError("backup snapshot failed")

    def wait(seconds):
        if len(exports.writes) == 1:
            raise KeyboardInterrupt()

    with pytest.raises(OSError if failure == "snapshot" else KeyboardInterrupt):
        run(exports, snapshot=snapshot if failure == "snapshot" else None,
            wait=wait if failure == "interrupt" else None)
    assert exports.value == "original"
    assert len(exports.writes) == 2


def test_restore_failure_is_explicit_and_not_retried():
    exports = Exports(failure="restore")
    records = []
    with pytest.raises(RuntimeError, match="restore RPC failed"):
        store_and_restore(exports, KEY, "test", None, records.append,
                          lambda label: None, lambda seconds: None)
    assert records[-1]["phase"] == "restore_failed"
    assert records[-1]["manual_recovery_required"]
    assert len(exports.writes) == 2


@pytest.mark.parametrize("original,restore", [("original", "wrong"), (None, None), ("test", None)])
def test_invalid_or_unrestorable_initial_state_fails_before_write(original, restore):
    exports = Exports(original=original)
    with pytest.raises(RuntimeError):
        run(exports, restore=restore)
    assert exports.writes == []


def test_output_failure_after_mutation_does_not_prevent_restore():
    exports = Exports()

    def record(item):
        if exports.writes:
            raise OSError("evidence disk full")

    with pytest.raises(OSError, match="disk full"):
        store_and_restore(exports, KEY, "test", None, record,
                          lambda label: None, lambda seconds: None)
    assert exports.writes == [(KEY, "test"), (KEY, "original")]
    assert exports.value == "original"
