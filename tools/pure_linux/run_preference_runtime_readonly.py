"""Bind explicit private material to a read-only isolated Android runtime capture."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import zipfile

from preference_identity_material import private_write


SCHEMA = "nashura.preference-runtime-readonly.v1"
PACKAGE = "nullsroyale.rel.free"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def remote(adb, serial, *arguments):
    return subprocess.run([str(adb), "-s", serial, "exec-out", shlex.join(arguments)],
                          capture_output=True, check=True, timeout=60).stdout


def process_identity(adb, serial, pid):
    command = remote(adb, serial, "cat", "/proc/%d/cmdline" % pid).rstrip(b"\x00\r\n")
    boot = remote(adb, serial, "cat", "/proc/sys/kernel/random/boot_id").decode().strip()
    fields = remote(adb, serial, "cat", "/proc/%d/stat" % pid).decode().rsplit(") ", 1)[1].split()
    if command != PACKAGE.encode() or not boot or len(fields) < 20:
        raise ValueError("isolated app process identity mismatch")
    return {"pid": pid, "starttime": int(fields[19]), "boot_id": boot, "package": PACKAGE}


def remote_sha(adb, serial, path):
    if not path.startswith("/") or "\x00" in path or "\n" in path:
        raise ValueError("explicit absolute remote path required")
    value = remote(adb, serial, "sha256sum", "--", path).decode().split()[0]
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid remote digest")
    return value


def validate_capture(capture, pid, material):
    if (capture.get("schema") != SCHEMA or capture.get("pid") != pid or
            capture.get("package") != PACKAGE or capture.get("charset") != "UTF-8" or
            not isinstance(capture.get("locale"), str) or not capture["locale"] or
            capture.get("device_derivation") != "SHA256(Settings.Secure android_id UTF-8)" or
            len(material) != 80):
        raise ValueError("runtime capture identity/domain mismatch")
    for name in ("stores_issued", "backup_requests_issued", "original_cipher_calls_issued"):
        if type(capture.get(name)) is not int or capture[name] != 0:
            raise ValueError("unexpected mutating operation")
    expected_keys = [sha256(material[:32]), sha256(material[32:64])]
    if capture.get("device_key_sha256") != expected_keys[0]:
        raise ValueError("live o key differs from observed device derivation")
    stores = capture.get("stores")
    if not isinstance(stores, list) or len(stores) != 2:
        raise ValueError("two runtime stores required")
    for index, slot in enumerate(("o", "p")):
        entry = stores[index]
        if entry.get("slot") != slot or set(entry.get("ciphers", {})) != {"a", "b", "c"}:
            raise ValueError("store/cipher mapping differs")
        for name, cipher in entry["ciphers"].items():
            expected = "AES/ECB/PKCS5Padding" if name == "c" else "AES/CBC/PKCS5Padding"
            if (cipher.get("algorithm") != expected or cipher.get("provider") != "AndroidOpenSSL" or
                    cipher.get("key_bytes") != 32 or cipher.get("key_sha256") != expected_keys[index] or
                    cipher.get("encrypting") is not (name != "b") or
                    cipher.get("called_update") is not False):
                raise ValueError("live Cipher profile differs")
            if (cipher.get("iv_bytes") != (0 if name == "c" else 16) or
                    cipher.get("iv_sha256") != (None if name == "c" else sha256(material[64:]))):
                raise ValueError("live Cipher IV differs")


def run(args, frida):
    source = args.script.read_bytes()
    old_material = args.historical_material.read_bytes()
    if (sha256(source) != args.script_sha256 or len(old_material) != 80 or
            sha256(old_material) != args.historical_material_sha256 or args.output.exists()):
        raise ValueError("input identity mismatch or output exists")
    with zipfile.ZipFile(args.apk) as archive:
        dex_sha = sha256(archive.read("classes.dex"))
    if sha256(args.apk.read_bytes()) != args.apk_sha256 or dex_sha != args.dex_sha256:
        raise ValueError("local APK/DEX mismatch")
    args.output.mkdir(mode=0o700)
    record = {"schema": "nashura.preference-runtime-binding.v1", "status": "FAILED",
              "script_sha256": args.script_sha256, "frida_version": frida.__version__,
              "historical_material_sha256": args.historical_material_sha256,
              "dex_sha256": dex_sha, "stores_issued": 0, "backup_requests_issued": 0,
              "configuration_contract_pass": False, "catalog_ready_pass": False}
    session = None
    paths = {"apk": args.remote_apk, "libg": args.remote_libg,
             "xml0": args.xml0, "xml1": args.xml1}
    try:
        record["process_before"] = process_identity(args.adb, args.serial, args.pid)
        record["files_before"] = {name: remote_sha(args.adb, args.serial, path)
                                  for name, path in paths.items()}
        if (record["files_before"]["apk"] != args.apk_sha256 or
                record["files_before"]["libg"] != args.libg_sha256):
            raise ValueError("installed runtime differs from explicit input")
        for index in range(2):
            raw = remote(args.adb, args.serial, "cat", paths["xml" + str(index)])
            if sha256(raw) != record["files_before"]["xml" + str(index)]:
                raise ValueError("XML changed during copy")
            private_write(args.output / ("original-%d.xml" % index), raw)
        device = frida.get_device(args.serial, timeout=15)
        if args.pid not in {process.pid for process in device.enumerate_processes()}:
            raise ValueError("PID absent from Frida")
        session = device.attach(args.pid)
        script = session.create_script(source.decode())
        script.load()
        observations = []
        for ordinal in range(2):
            capture = script.exports_sync.inspect()
            material = bytes.fromhex(capture.pop("private_material_hex"))
            validate_capture(capture, args.pid, material)
            if (capture.get("source_apk") != args.remote_apk or
                    capture.get("native_libg") != args.remote_libg or
                    [item["path"] for item in capture["stores"]] != [args.xml0, args.xml1]):
                raise ValueError("runtime backing paths differ")
            observations.append(capture)
            if ordinal == 0:
                private_write(args.output / "material.bin", material)
            elif (args.output / "material.bin").read_bytes() != material:
                raise ValueError("runtime material changed")
        if observations[0] != observations[1]:
            raise ValueError("read-only observations differ")
        record["observations"] = observations
        record["material_sha256"] = sha256(material)
        record["historical_matches"] = {"o": old_material[:32] == material[:32],
                                        "p": old_material[32:64] == material[32:64],
                                        "iv": old_material[64:] == material[64:]}
        record["files_after"] = {name: remote_sha(args.adb, args.serial, path)
                                 for name, path in paths.items()}
        record["process_after"] = process_identity(args.adb, args.serial, args.pid)
        if (record["files_before"] != record["files_after"] or
                record["process_before"] != record["process_after"] or
                args.script.read_bytes() != source or args.historical_material.read_bytes() != old_material):
            raise ValueError("input/runtime identity changed during observation")
        record["status"] = "RUNTIME_OBSERVED_LIMITED"
    except BaseException as error:
        record["error_type"] = type(error).__name__
        raise
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception as error:
                record["detach_error_type"] = type(error).__name__
                record["status"] = "INCOMPLETE_COVERAGE"
        checks = {}
        for name, operation in {
                "files_final": lambda: {name: remote_sha(args.adb, args.serial, path)
                                         for name, path in paths.items()},
                "process_final": lambda: process_identity(args.adb, args.serial, args.pid),
                "local_inputs_unchanged": lambda: (
                    args.script.read_bytes() == source and
                    args.historical_material.read_bytes() == old_material and
                    sha256(args.apk.read_bytes()) == args.apk_sha256)}.items():
            try:
                record[name] = operation()
                checks[name] = (record[name] == record.get("files_before") if name == "files_final"
                                else record[name] == record.get("process_before") if name == "process_final"
                                else record[name] is True)
            except Exception as error:
                record[name + "_error_type"] = type(error).__name__
                checks[name] = False
        record["final_integrity_checks"] = checks
        if not all(checks.values()) and record["status"] == "RUNTIME_OBSERVED_LIMITED":
            record["status"] = "INCOMPLETE_COVERAGE"
        private_write(args.output / "result.json", (json.dumps(record, indent=2) + "\n").encode())
    return record


def main():
    import frida

    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("adb", "script", "historical-material", "apk", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("script-sha256", "historical-material-sha256", "apk-sha256", "dex-sha256",
                 "libg-sha256", "serial", "remote-apk", "remote-libg", "xml0", "xml1"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    record = run(args, frida)
    print(json.dumps({"status": record["status"], "historical_matches": record["historical_matches"],
                      "configuration_contract_pass": False, "catalog_ready_pass": False}))
    return 0 if record["status"] == "RUNTIME_OBSERVED_LIMITED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
