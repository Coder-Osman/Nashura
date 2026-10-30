"""Fixed ELF boundary and exact profile-command checks, not runtime evidence."""
import hashlib
import struct
from types import SimpleNamespace

import pytest

import verify_combined_profile as profile
from test_summarize_headless_perf import inputs


def elf_input(monkeypatch):
    data = bytearray(512)
    struct.pack_into("<4BIIiiii", data, 0, 1, 0x1b, 3, 0x3b, 0, 2, 100, 300, 200, 340)
    for fde, start, size in ((300, 100, 20), (340, 200, 20)):
        struct.pack_into("<IIiI", data, fde, 12, 0, start - fde - 8, size)
    data = bytes(data)
    monkeypatch.setattr(profile, "ENGINE_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(profile, "TARGETS", {"test": 104})
    monkeypatch.setattr(profile, "Elf64", lambda value: SimpleNamespace(
        segments=[{"kind":0x6474e550,"offset":0,"virtual":0,"filesz":28}],
        slice=lambda offset,size:value[offset:offset+size],address=lambda offset,size:offset))
    return data


def test_fixed_fde_and_index_resolve_same_function(monkeypatch):
    data = elf_input(monkeypatch)
    result = profile.function_regions(data)
    assert result["test"] == {"start":"0x64","end":"0x78",
        "original_bytes_sha256":hashlib.sha256(data[100:120]).hexdigest()}


@pytest.mark.parametrize("mutation", ["sha", "encoding", "count", "order", "fde", "range"])
def test_elf_identity_and_unwind_structure_are_strict(monkeypatch,mutation):
    data = bytearray(elf_input(monkeypatch))
    if mutation == "sha":
        data[499] = 1
    else:
        if mutation == "encoding":data[1] = 0
        elif mutation == "count":struct.pack_into("<I",data,8,3)
        elif mutation == "order":struct.pack_into("<i",data,20,100)
        elif mutation == "fde":struct.pack_into("<i",data,308,1)
        elif mutation == "range":struct.pack_into("<I",data,312,101)
        monkeypatch.setattr(profile,"ENGINE_SHA256",hashlib.sha256(data).hexdigest())
    with pytest.raises(ValueError):profile.function_regions(bytes(data))


def test_regions_count_only_mainloop_leaf_in_measured_windows(inputs):
    log = inputs["log"]
    # This helper has the fixed production 16-match warmup; prefix synthetic
    # measured records to exercise its exclusion without changing the contract.
    first = next(x for x in log.splitlines() if x.startswith("HEADLESS_BATTLE_METRICS "))
    log = (first + chr(10)) * 16 + log
    regions={"query":{"start":"0xf34b3c","end":"0xf35064"}}
    assert profile.region_counts(log,inputs["script"],inputs["mappings"],regions)=={"query":2}


def test_missing_profile_command_rejected():
    with pytest.raises(ValueError):profile.validate_command([],profile.CASES[0])
