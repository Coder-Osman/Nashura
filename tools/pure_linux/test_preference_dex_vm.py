import pytest

import preference_differential as differential
from preference_dex_vm import DexMachine, SELECTED, parse_methods
from preference_model import PlatformFailure, Storage


def program(instructions, catches=()):
    return {"fixture": {"registers": 2, "ins": 1, "instructions": instructions, "catches": catches}}


def test_branch_uses_target_instead_of_next_instruction():
    methods = program({0: "if-eqz v1, 0004 // +0004", 2: 'const-string v0, "nonzero"',
                       3: "return-object v0", 4: 'const-string v0, "zero"', 6: "return-object v0"})
    assert DexMachine(methods, Storage()).execute("fixture", [0]) == "zero"
    assert DexMachine(methods, Storage()).execute("fixture", [1]) == "nonzero"


def test_invalid_branch_displacement_and_non_boundary_fail_closed():
    for branch in ("if-eqz v1, 0004 // +0002", "if-eqz v1, 0005 // +0005"):
        methods = program({0: branch, 2: "return-void", 4: "return-void"})
        with pytest.raises(ValueError):
            DexMachine(methods, Storage()).execute("fixture", [0])


def test_catch_range_is_end_exclusive():
    instructions = {0: "invoke-static {}, Lcom/supercell/titan/GameApp;.requestBackup:()V",
                    3: "return-void", 4: 'const-string v0, "caught"', 6: "return-object v0"}
    with pytest.raises(PlatformFailure):
        DexMachine(program(instructions, [(3, 4, 4)]), Storage(failures=[1])).execute("fixture", [0])
    assert DexMachine(program(instructions, [(0, 3, 4)]), Storage(failures=[1])).execute("fixture", [0]) == "caught"


def test_unsupported_call_and_instruction_and_loop_are_rejected():
    for instruction in ("invoke-static {}, Lfixture;.unknown:()V", "nop", "goto 0000 // +0000"):
        with pytest.raises(ValueError):
            DexMachine(program({0: instruction}), Storage()).execute("fixture", [0])


def fixture_dump():
    return "\n".join("  registers : 1\n  ins : 1\n000000: |[000000] " + name +
                     "\n000010: |0000: return-void\n catches : (none)\n positions     :" for name in SELECTED)


def test_parser_requires_all_methods_and_valid_catch_target():
    assert set(parse_methods(fixture_dump())) == set(SELECTED)
    for text in (fixture_dump() + "\n" + fixture_dump(),
                 fixture_dump().replace(SELECTED[0], "fixture.missing:()V"),
                 fixture_dump().replace("catches : (none)",
                                        "catches : 1\n0x0000 - 0x0001\nLjava/lang/Exception; -> 0x0004")):
        with pytest.raises(ValueError):
            parse_methods(text)


@pytest.mark.parametrize("invalid_input", ["dex", "dexdump", "native-library"])
def test_differential_rejects_wrong_identity_before_external_execution(tmp_path, monkeypatch, invalid_input):
    arguments = ["differential"]
    for name in ("dex", "dexdump", "native-library"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        expected = "0" * 64 if name == invalid_input else differential.digest(path.read_bytes())
        arguments.extend(["--" + name, str(path), "--" + name + "-sha256", expected])
    output = tmp_path / "evidence.json"
    arguments.extend(["--output", str(output)])
    monkeypatch.setattr("sys.argv", arguments)
    monkeypatch.setattr(differential.subprocess, "run", lambda *args, **kwargs: pytest.fail("external process started"))
    with pytest.raises(ValueError, match="identity mismatch"):
        differential.main()
    assert not output.exists()


def test_differential_preserves_existing_evidence(tmp_path, monkeypatch):
    arguments = ["differential"]
    for name in ("dex", "dexdump", "native-library"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        arguments.extend(["--" + name, str(path), "--" + name + "-sha256",
                          differential.digest(path.read_bytes())])
    output = tmp_path / "evidence.json"
    output.write_bytes(b"historical evidence")
    arguments.extend(["--output", str(output)])
    monkeypatch.setattr("sys.argv", arguments)
    monkeypatch.setattr(differential.subprocess, "run", lambda *args, **kwargs: pytest.fail("external process started"))
    with pytest.raises(ValueError, match="already exists"):
        differential.main()
    assert output.read_bytes() == b"historical evidence"
