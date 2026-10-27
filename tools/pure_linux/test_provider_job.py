import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from provider_job import catalog_observations, registration_observations, resource_inventory, resource_observations, strace_launch


def registration_log():
    rows = []
    for name, table in [("data", "18c3370"), ("update", "18c3400")]:
        rows.extend([f"PHASE\tregister_return\t{name}", f"PROVIDER_SLOT\t{name}\t1\t2",
                     f"PROVIDER_VTABLE\t{name}\t{table}",
                     f"PROVIDER_STRING\t{name}\t8\t1\t/",
                     f"PROVIDER_STRING\t{name}\t32\t{len(name) + 1}\t{name}:", "STRING_GUARD\t1"])
    return "\n".join(rows + ["PROVIDER_CONTENT_READY\t0", "PROVIDER_SLOTS_RELEASED\t1",
                             "PHASE\tprovider_release_return", "PROVIDER_RESULT\t0", "PHASE\tafter_close"])


def catalog_log():
    return "\n".join(["CATALOG_LIST\t224\t158", "PHASE\tcatalog_preload_return",
                      "CATALOG_PRELOAD\t3\t368", "CATALOG_READY_AFTER_PRELOAD\t0",
                      "LISTENER_CANARY\t1", "PHASE\tlistener_destructor_return",
                      *[f"CATALOG_LOAD_RETURN\t{start}\t{int(start == 156)}\t0"
                        for start in [0, 39, 78, 117, 156]], "CATALOG_TABLE_LOAD_COMPLETE\t1"])


def test_registration_release_and_identity():
    assert registration_observations(registration_log(), "both")["complete"]


@pytest.mark.parametrize("damage", [
    lambda text: text + "\nPHASE\tregister_return\tdata",
    lambda text: text + "\nPROVIDER_STRING\tdata\t32\t5\tdata:",
    lambda text: text + "\nPROVIDER_STRING\tdata\t8\t1\t/",
    lambda text: text.replace("PROVIDER_STRING\tdata\t8\t1\t/", ""),
    lambda text: text.replace("PROVIDER_STRING\tdata\t8\t1\t/", "PROVIDER_STRING\tdata\t8\t2\t/"),
    lambda text: text + "\nPROVIDER_SLOT",
    lambda text: text.replace("PROVIDER_SLOT\tdata\t1", "PROVIDER_SLOT\tdata\t0"),
    lambda text: text.replace("18c3370", "18c3400"),
    lambda text: text.replace("PROVIDER_CONTENT_READY\t0", "PROVIDER_CONTENT_READY\t1"),
    lambda text: text.replace("STRING_GUARD\t1", "STRING_GUARD\t0"),
    lambda text: text.replace("PHASE\tprovider_release_return", "missing"),
])
def test_registration_rejects_incomplete_or_ambiguous(damage):
    assert not registration_observations(damage(registration_log()), "both")["complete"]


def test_tables_are_not_catalog_ready():
    result = catalog_observations(catalog_log(), "load")
    assert result["complete"] and not result["catalog_ready_pass"]


@pytest.mark.parametrize("old,new", [
    ("CATALOG_LOAD_RETURN\t39\t0\t0", "CATALOG_LOAD_RETURN\t39\t1\t0"),
    ("CATALOG_LOAD_RETURN\t78\t0\t0", ""),
    ("CATALOG_LOAD_RETURN\t156\t1\t0", "CATALOG_LOAD_RETURN\t156\t1\t1"),
    ("CATALOG_PRELOAD\t3\t368", "CATALOG_PRELOAD\t2\t368"),
    ("CATALOG_PRELOAD\t3\t368", "CATALOG_PRELOAD\t3\t0"),
])
def test_table_completion_requires_all_batches(old, new):
    assert not catalog_observations(catalog_log().replace(old, new), "load")["complete"]


def test_loader_construct_does_not_claim_start_or_ready():
    text = catalog_log() + "\nPHASE\tloader_constructor_return\nPHASE\tloader_destructor_return\nLOADER_CANARY\t1\nCATALOG_READY_AFTER_LOADER\t0"
    assert catalog_observations(text, "loader-construct")["complete"]
    assert not catalog_observations(text + "\nPHASE\tloader_start_enter", "loader-construct")["complete"]
    assert not catalog_observations(text, "loader")["complete"]


def test_resource_inventory_rejects_symlink_and_root(tmp_path):
    (tmp_path / "asset").write_bytes(b"data")
    original = resource_inventory({"assets": tmp_path})
    (tmp_path / "asset").write_bytes(b"changed")
    assert original != resource_inventory({"assets": tmp_path})
    (tmp_path / "link").symlink_to(tmp_path / "asset")
    with pytest.raises(ValueError):
        resource_inventory({"assets": tmp_path})
    with pytest.raises(ValueError):
        resource_inventory({"assets": Path("/")})


def test_missing_resource_log_is_not_success(tmp_path):
    assert not resource_observations(tmp_path, "", None)["complete"]


def resource_log(kind):
    return "\n".join([f"RESOURCE_FILE_KIND\t{kind}", "RESOURCE_FILE_VTABLE\t1",
                      "RESOURCE_VALID_LENGTH\t1\t7", "RESOURCE_READ\t3", "RESOURCE_READ\t4",
                      "RESOURCE_SEEK_RESULT\t0", "RESOURCE_SEMANTICS\t0\t7\t1\t0\t1\t1",
                      "PHASE\tresource_close_return", "RESOURCE_BYTES\t7"])


@pytest.mark.parametrize("kind", ["apk", "disk"])
def test_resource_checks_full_bytes_and_file_contract(tmp_path, kind):
    content = b"abc\x00def"
    (tmp_path / "resource.bin").write_bytes(content)
    assert resource_observations(tmp_path, resource_log(kind), hashlib.sha256(content).hexdigest())["complete"]


@pytest.mark.parametrize("old,new", [
    ("RESOURCE_READ\t4", "RESOURCE_READ\t3"),
    ("RESOURCE_READ\t4", "RESOURCE_READ\t-1"),
    ("RESOURCE_READ\t4", "RESOURCE_READ\tx"),
    ("RESOURCE_READ\t4", "RESOURCE_READ"),
    ("RESOURCE_VALID_LENGTH\t1\t7", "RESOURCE_VALID_LENGTH\t1\tx"),
    ("RESOURCE_VALID_LENGTH\t1\t7", ""),
    ("RESOURCE_VALID_LENGTH\t1\t7", "RESOURCE_VALID_LENGTH\t1\t7\nRESOURCE_VALID_LENGTH\t0\t7"),
    ("RESOURCE_FILE_KIND\tapk", "RESOURCE_FILE_KIND\tunknown"),
    ("RESOURCE_FILE_VTABLE\t1", "RESOURCE_FILE_VTABLE\t1\nRESOURCE_FILE_VTABLE\t0"),
    ("RESOURCE_SEEK_RESULT\t0", "RESOURCE_SEEK_RESULT\t-1"),
    ("RESOURCE_SEMANTICS\t0\t7\t1\t0\t1\t1", "RESOURCE_SEMANTICS\t0\t7\t1\t1\t1\t1"),
    ("PHASE\tresource_close_return", ""),
    ("PHASE\tresource_close_return", "PHASE\tresource_close_return\nPHASE\tresource_close_return"),
    ("RESOURCE_BYTES\t7", "RESOURCE_BYTES\t6"),
])
def test_resource_rejects_damaged_observation(tmp_path, old, new):
    content = b"abc\x00def"
    (tmp_path / "resource.bin").write_bytes(content)
    assert not resource_observations(tmp_path, resource_log("apk").replace(old, new),
                                     hashlib.sha256(content).hexdigest())["complete"]


def test_resource_rejects_changed_bytes_and_missing_identity(tmp_path):
    original = b"abc\x00def"
    (tmp_path / "resource.bin").write_bytes(b"abc\x00deg")
    assert not resource_observations(tmp_path, resource_log("disk"), hashlib.sha256(original).hexdigest())["complete"]
    assert not resource_observations(tmp_path, resource_log("disk"), None)["complete"]


def test_strace_keeps_android_loader_environment_out_of_host(tmp_path):
    environment = {"PATH": "/usr/bin", "LD_LIBRARY_PATH": "/android/libs",
                   "LD_PRELOAD": "/android/backend.so", "NASHURA_PROVIDER_MODE": "both"}
    original = dict(environment)
    inferior = ["/android/linker64", "/android/probe", "/android/libs/libg.so"]
    command, host_environment = strace_launch(inferior, tmp_path, environment)
    assert host_environment == {"PATH": "/usr/bin", "NASHURA_PROVIDER_MODE": "both"}
    assert command[-3:] == inferior
    assert command[command.index("-E"):] == ["-E", "LD_LIBRARY_PATH=/android/libs",
                                            "-E", "LD_PRELOAD=/android/backend.so", *inferior]
    assert environment == original


@pytest.mark.skipif(sys.platform != "linux", reason="owned-child SIGSTOP observation requires Linux")
def test_launcher_attaches_only_to_its_stopped_child(tmp_path):
    fake = tmp_path / "gdb"
    output = tmp_path / "observed.json"
    fake.write_text("#!" + sys.executable + "\nimport json,os,signal\n"
                    "pid=int(os.environ['NASHURA_GDB_OWNED_PID'])\n"
                    "open(os.environ['TEST_OUTPUT'],'w').write(json.dumps({'pid':pid,'args':__import__('sys').argv}))\n"
                    "os.kill(pid,signal.SIGKILL)\n")
    fake.chmod(0o700)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"inferior": [sys.executable, "-c", "import os,signal;os.kill(os.getpid(),signal.SIGSTOP)"],
                                "environment": dict(os.environ), "script": "test.py"}))
    environment = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"], TEST_OUTPUT=str(output))
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("native_init_attach.py")), str(spec)],
                            env=environment, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    observed = json.loads(output.read_text())
    assert observed["args"][observed["args"].index("-p") + 1] == str(observed["pid"])


def test_launcher_rejects_early_exit(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"inferior": [sys.executable, "-c", "raise SystemExit(0)"],
                                "environment": dict(os.environ), "script": "not-reached.py"}))
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("native_init_attach.py")), str(spec)],
                            capture_output=True, timeout=20)
    assert result.returncode != 0
    assert b"did not reach SIGSTOP" in result.stderr
