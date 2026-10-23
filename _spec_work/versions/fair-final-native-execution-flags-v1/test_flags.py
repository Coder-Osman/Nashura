from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from audit_flags import (AuditError, CAPTURE, READS, RETIREMENTS, ROOT,
    audit, command_audit, flag_lifetime)


@dataclass
class Instruction:
    address: int
    mnemonic: str = "nop"
    op_str: str = ""
    reads: tuple = ()
    writes: tuple = ()
    operands: list = field(default_factory=list)

    def regs_access(self):
        return self.reads, self.writes

    def reg_name(self, register):
        return register


def fixture():
    instructions = [Instruction(address) for address in
        range(command_audit.EXECUTOR, command_audit.EXECUTOR_END, 4)]
    def put(address, mnemonic, operands="", reads=(), writes=(), target=None):
        value = Instruction(address, mnemonic, operands, reads, writes)
        if target is not None:
            value.operands = [SimpleNamespace(type=command_audit.capstone.arm64.ARM64_OP_IMM, imm=target)]
        instructions[(address - command_audit.EXECUTOR) // 4] = value
    put(CAPTURE, "mov", "w23, w2", reads=("w2",), writes=("w23",))
    put(CAPTURE + 4, "cbz", "w0, #0xd30c68", target=0xD30C68)
    put(CAPTURE + 8, "b", "#0xd30ab0", target=0xD30AB0)
    for address, (mnemonic, operands) in READS.items():
        put(address, mnemonic, operands, reads=("w23",), target=int(operands.rsplit("#", 1)[1], 16))
    for address, (mnemonic, operands) in RETIREMENTS.items():
        put(address, mnemonic, operands, writes=("x23",))
    put(0xD30AB4, "ret")
    put(0xD30E18, "ret")
    put(command_audit.EXECUTOR_END - 4, "ret")
    return instructions


def test_synthetic_lifetime_covers_both_bit_tests_and_retirements():
    report = flag_lifetime(fixture())
    assert report["bit0_used"] is False
    assert report["observed_bit_indices"] == [1]
    assert {int(row["address"], 16) for row in report["reads"]} == set(READS)
    assert {int(row["address"], 16) for row in report["retirements"]} == set(RETIREMENTS)


@pytest.mark.parametrize("register", ["w23", "x23"])
def test_unclassified_live_alias_read_is_rejected(register):
    values = fixture()
    values[(CAPTURE + 12 - command_audit.EXECUTOR) // 4].reads = (register,)
    values[(CAPTURE + 8 - command_audit.EXECUTOR) // 4].mnemonic = "nop"
    with pytest.raises(AuditError, match="unclassified live argument read"):
        flag_lifetime(values)


@pytest.mark.parametrize("register", ["w23", "x23"])
def test_unclassified_live_alias_write_is_rejected(register):
    values = fixture()
    values[(CAPTURE + 12 - command_audit.EXECUTOR) // 4].writes = (register,)
    values[(CAPTURE + 8 - command_audit.EXECUTOR) // 4].mnemonic = "nop"
    with pytest.raises(AuditError, match="unclassified argument retirement"):
        flag_lifetime(values)


@pytest.mark.parametrize("address", sorted(READS))
def test_changed_bit_is_rejected(address):
    values = fixture()
    values[(address - command_audit.EXECUTOR) // 4].op_str = "w23, #0, #0xd30dd8"
    with pytest.raises(AuditError, match="unclassified live argument read"):
        flag_lifetime(values)


@pytest.mark.parametrize("address", sorted(RETIREMENTS))
def test_changed_retirement_is_rejected(address):
    values = fixture()
    values[(address - command_audit.EXECUTOR) // 4].op_str = "x24, x23, [sp, #0x50]"
    with pytest.raises(AuditError, match="unclassified argument retirement"):
        flag_lifetime(values)


def test_changed_capture_is_rejected():
    values = fixture()
    values[(CAPTURE - command_audit.EXECUTOR) // 4].op_str = "w23, w3"
    with pytest.raises(AuditError, match="argument capture differs"):
        flag_lifetime(values)


def test_missing_expected_path_is_rejected():
    values = fixture()
    instruction = values[(CAPTURE + 4 - command_audit.EXECUTOR) // 4]
    instruction.mnemonic = "b"
    with pytest.raises(AuditError, match="coverage differs"):
        flag_lifetime(values)


def test_early_return_with_live_argument_is_rejected():
    values = fixture()
    instruction = values[(CAPTURE + 8 - command_audit.EXECUTOR) // 4]
    instruction.mnemonic, instruction.operands = "ret", []
    with pytest.raises(AuditError, match="exits before checked argument retirement"):
        flag_lifetime(values)


def test_incomplete_cfg_is_rejected():
    with pytest.raises(AuditError):
        flag_lifetime(fixture()[:-1])


def test_frozen_binary_argument_analysis():
    library = ROOT.parent / "fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so"
    if not library.exists():
        pytest.skip("private frozen native library not present")
    report = audit(library)
    assert report["conclusions"]["explicit_bit1_choices_match"] is True
    assert report["conclusions"]["whole_executor_equivalence_proven"] is False
    assert report["conclusions"]["native_rejection_reachability_proven"] is False
    assert report["training_admissible"] is False
    assert report["gates_closed"] is False


def test_wrong_binary_is_rejected(tmp_path):
    library = tmp_path / "wrong.so"
    library.write_bytes(b"wrong identity")
    with pytest.raises(AuditError, match="unexpected library SHA"):
        audit(library)
