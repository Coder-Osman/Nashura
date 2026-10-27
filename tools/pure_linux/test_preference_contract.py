import ctypes
from pathlib import Path
import shutil
import subprocess

import pytest

from preference_model import (KEY, CAPACITY, Backend, Backup, Storage, configure, invoke, encoded)


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    compiler = shutil.which("clang") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler required")
    source = Path(__file__).with_name("preference_contract.c")
    output = tmp_path_factory.mktemp("preference-contract") / "contract.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                    str(source), "-o", str(output)], check=True)
    return configure(output)


def test_false_commit_is_ignored_without_coalescing_slots(library):
    storage = Storage(false_commits=[1])
    assert invoke(library, storage, "write", "datatables-init")[0] == 0
    assert storage.memory == [{encoded(0): "cipher:datatables-init"}, {encoded(1): "cipher:datatables-init"}]
    assert storage.disk == [{}, {encoded(1): "cipher:datatables-init"}]
    assert storage.commits == 2
    assert storage.events[-1] == ["backup"]
    assert invoke(library, storage, "read") == (0, "datatables-init")


def test_legacy_and_encoded_deletions_are_four_separate_commits(library):
    storage = Storage([{KEY: "legacy", encoded(0): "cipher:old"},
                       {KEY: "legacy", encoded(1): "cipher:old"}])
    assert invoke(library, storage, "write", None)[0] == 0
    assert storage.commits == 4
    assert storage.memory == [{}, {}]
    changed_keys = [(event[1], event[3]) for event in storage.events if event[0] == "change"]
    assert changed_keys == [(1, KEY), (2, encoded(0)), (3, KEY), (4, encoded(1))]


def test_legacy_only_is_not_read_or_deleted_by_equal_empty_write(library):
    storage = Storage([{KEY: "legacy"}, {KEY: "legacy"}])
    assert invoke(library, storage, "read") == (0, "")
    assert invoke(library, storage, "write", "")[0] == 0
    assert storage.commits == 0
    assert storage.memory == [{KEY: "legacy"}, {KEY: "legacy"}]
    assert invoke(library, storage, "write", None)[0] == 0
    assert storage.memory == [{}, {}]


def test_decode_failure_cleans_before_read_repair(library):
    storage = Storage([{encoded(0): "cipher:recovered"}, {encoded(1): "bad"}], false_commits=[1])
    assert invoke(library, storage, "read") == (0, "recovered")
    assert storage.commits == 2
    names = [event[0] for event in storage.events]
    assert names.index("decode_exception") < names.index("commit") < names.index("backup")
    assert storage.memory[1] == {encoded(1): "cipher:recovered"}


def test_get_string_type_error_is_not_caught_as_decrypt_failure(library):
    storage = Storage([{}, {encoded(1): 123}])
    assert invoke(library, storage, "read") == (-1, "UNASSIGNED")
    assert not any(event[0] in {"decode_exception", "edit", "backup"} for event in storage.events)


def test_exception_within_decrypt_boundary_is_caught_and_cleans_value(library):
    storage = Storage([{}, {encoded(1): "cipher:old"}], failures=[4])
    assert invoke(library, storage, "read") == (0, "")
    assert storage.memory == [{}, {}]
    assert storage.events[4] == ["decode_exception", 1]
    assert storage.commits == 1


def test_backup_failure_preserves_already_committed_slots(library):
    reference = Storage()
    assert invoke(library, reference, "write", "value")[0] == 0
    storage = Storage(failures=[len(reference.events)])
    assert invoke(library, storage, "write", "value")[0] == -1
    assert storage.disk == reference.disk
    assert storage.events[-1] == ["backup"]


def test_exception_at_every_callback_boundary_stops_without_next_slot_or_backup(library):
    reference = Storage()
    assert invoke(library, reference, "write", "value")[0] == 0
    for failure in range(1, len(reference.events) + 1):
        storage = Storage(failures=[failure])
        assert invoke(library, storage, "write", "value")[0] == -1
        assert storage.events == reference.events[:failure]
        completed = sum(event[0] == "commit" for event in reference.events[:failure - 1])
        assert storage.commits == completed


def test_missing_backend_and_unknown_key_are_explicit_failures(library):
    storage = Storage()
    assert invoke(library, storage, "write", "value", key="UNKNOWN")[0] == -1
    assert storage.events == []
    adapter = storage.adapter()
    adapter.request_backup = Backup()
    assert library.preference_contract_write(ctypes.byref(adapter), KEY.encode(), b"value") == -1
    assert storage.events == []
