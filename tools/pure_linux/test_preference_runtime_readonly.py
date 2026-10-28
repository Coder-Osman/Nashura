from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

import run_preference_runtime_readonly as subject


def capture(material):
    result = {"schema": subject.SCHEMA, "pid": 71, "package": subject.PACKAGE,
              "charset": "UTF-8", "locale": "en-US",
              "device_derivation": "SHA256(Settings.Secure android_id UTF-8)",
              "device_key_sha256": subject.sha256(material[:32]),
              "stores_issued": 0, "backup_requests_issued": 0,
              "original_cipher_calls_issued": 0, "source_apk": "/fixture.apk",
              "native_libg": "/libg.so", "stores": []}
    for index, slot in enumerate(("o", "p")):
        ciphers = {}
        for name in ("a", "b", "c"):
            ciphers[name] = {"algorithm": "AES/ECB/PKCS5Padding" if name == "c" else "AES/CBC/PKCS5Padding",
                             "provider": "AndroidOpenSSL", "key_bytes": 32,
                             "key_sha256": subject.sha256(material[index * 32:(index + 1) * 32]),
                             "encrypting": name != "b", "called_update": False,
                             "iv_bytes": 0 if name == "c" else 16,
                             "iv_sha256": None if name == "c" else subject.sha256(material[64:])}
        result["stores"].append({"slot": slot, "path": "/%d.xml" % index, "ciphers": ciphers})
    return result


@pytest.mark.parametrize("damage", ["none", "key", "iv", "algorithm", "direction", "update",
                                    "provider", "slot", "missing", "stores", "backup", "cipher",
                                    "bool", "device", "charset", "pid", "short"])
def test_profile_and_mutating_operations_fail_closed(damage):
    material = bytes(range(80))
    value = capture(material)
    cipher = value["stores"][0]["ciphers"]["a"]
    if damage == "key": cipher["key_sha256"] = "0" * 64
    if damage == "iv": cipher["iv_sha256"] = "0" * 64
    if damage == "algorithm": cipher["algorithm"] = "unknown"
    if damage == "direction": cipher["encrypting"] = False
    if damage == "update": cipher["called_update"] = True
    if damage == "provider": cipher["provider"] = "unknown"
    if damage == "slot": value["stores"][1]["slot"] = "o"
    if damage == "missing": value["stores"][0]["ciphers"].pop("b")
    if damage == "stores": value["stores_issued"] = 1
    if damage == "backup": value["backup_requests_issued"] = 1
    if damage == "cipher": value["original_cipher_calls_issued"] = 1
    if damage == "bool": value["stores_issued"] = False
    if damage == "device": value["device_key_sha256"] = "0" * 64
    if damage == "charset": value["charset"] = "unknown"
    if damage == "pid": value["pid"] = 72
    if damage == "short": material = material[:-1]
    if damage == "none":
        subject.validate_capture(value, 71, material)
    else:
        with pytest.raises(ValueError):
            subject.validate_capture(value, 71, material)


def setup_runtime(tmp_path, monkeypatch, damage):
    material = bytes(range(80))
    script_path = tmp_path / "probe.js"
    script_path.write_text("synthetic fixture")
    history = tmp_path / "history.bin"
    history.write_bytes(bytes([255]) * 32 + material[32:])
    apk = tmp_path / "fixture.apk"
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("classes.dex", b"synthetic dex")
    args = SimpleNamespace(script=script_path, script_sha256=subject.sha256(script_path.read_bytes()),
                           historical_material=history, historical_material_sha256=subject.sha256(history.read_bytes()),
                           apk=apk, apk_sha256=subject.sha256(apk.read_bytes()),
                           dex_sha256=subject.sha256(b"synthetic dex"), libg_sha256="7" * 64,
                           output=tmp_path / "out", remote_apk="/fixture.apk", remote_libg="/libg.so",
                           xml0="/0.xml", xml1="/1.xml", adb=Path("/fixture/adb"), serial="fixture", pid=71)
    state = {"calls": 0, "detached": False, "identity_calls": 0}
    files = {"/fixture.apk": args.apk_sha256, "/libg.so": args.libg_sha256,
             "/0.xml": subject.sha256(b"synthetic xml0"), "/1.xml": subject.sha256(b"synthetic xml1")}

    def inspect():
        state["calls"] += 1
        if damage == "rpc":
            raise RuntimeError("private-material-must-not-appear")
        value = deepcopy(capture(material))
        value["private_material_hex"] = material.hex()
        if damage == "paths": value["native_libg"] = "/different.so"
        if damage == "changed" and state["calls"] == 2: value["locale"] = "en-GB"
        return value

    def identity(*unused):
        state["identity_calls"] += 1
        return {"pid": 71, "starttime": 1 if damage != "reuse" or state["identity_calls"] == 1 else 2}

    def detach():
        state["detached"] = True
        if damage == "detach": raise RuntimeError("private-material-must-not-appear")

    script = SimpleNamespace(load=lambda: None, exports_sync=SimpleNamespace(inspect=inspect))
    session = SimpleNamespace(create_script=lambda source: script, detach=detach)
    device = SimpleNamespace(enumerate_processes=lambda: [SimpleNamespace(pid=71)], attach=lambda pid: session)
    frida = SimpleNamespace(__version__="fixture", get_device=lambda serial, timeout: device)
    monkeypatch.setattr(subject, "process_identity", identity)
    monkeypatch.setattr(subject, "remote_sha", lambda adb, serial, path: files[path])
    monkeypatch.setattr(subject, "remote", lambda adb, serial, operation, path: b"synthetic xml" + path[1:2].encode())
    return args, frida, state


@pytest.mark.parametrize("damage", ["none", "rpc", "paths", "changed", "reuse", "detach"])
def test_capture_preserves_failures_integrity_and_private_outputs(tmp_path, monkeypatch, damage):
    args, frida, state = setup_runtime(tmp_path, monkeypatch, damage)
    if damage in {"rpc", "paths", "changed", "reuse"}:
        with pytest.raises((ValueError, RuntimeError)):
            subject.run(args, frida)
    else:
        result = subject.run(args, frida)
        assert result["status"] == ("INCOMPLETE_COVERAGE" if damage == "detach" else "RUNTIME_OBSERVED_LIMITED")
    assert state["detached"]
    result = json.loads((args.output / "result.json").read_text())
    assert set(result["final_integrity_checks"]) == {"files_final", "process_final", "local_inputs_unchanged"}
    assert result["configuration_contract_pass"] is result["catalog_ready_pass"] is False
    assert "private-material-must-not-appear" not in (args.output / "result.json").read_text()
    assert "private_material_hex" not in (args.output / "result.json").read_text()
    assert bytes(range(80)).hex() not in (args.output / "result.json").read_text()
    assert args.output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in args.output.iterdir())
    before = (args.output / "result.json").read_bytes()
    with pytest.raises(ValueError):
        subject.run(args, frida)
    assert (args.output / "result.json").read_bytes() == before


def test_pid_identity_uses_start_time_and_exact_command(monkeypatch):
    responses = [subject.PACKAGE.encode() + b"\x00", b"fixture-boot\n",
                 ("71 (fixture) " + " ".join(["0"] * 19 + ["412"]) + "\n").encode()]
    monkeypatch.setattr(subject, "remote", lambda *args: responses.pop(0))
    assert subject.process_identity("adb", "fixture", 71)["starttime"] == 412


@pytest.mark.parametrize("path", ["relative", "/newline\nfile", "/null\x00file"])
def test_invalid_remote_path_rejected_without_subprocess(path, monkeypatch):
    monkeypatch.setattr(subject, "remote", lambda *args: pytest.fail("unexpected subprocess"))
    with pytest.raises(ValueError):
        subject.remote_sha("adb", "fixture", path)
