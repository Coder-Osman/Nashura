"""Observe original Android init and one real property-service request in Linux.

Run only in the existing ARM64 guest with the matching extracted SDK closure.
This probe does not load SELinux policy, write host sysctls, or bootstrap properties.
An acknowledgement alone is not property readback or SystemServer readiness.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import threading


# Android bionic system_properties.h and init/property_service.cpp, Android 11.
PROP_MSG_SETPROP2 = 0x00020001
PROP_ERROR_PERMISSION_DENIED = 0x0018
PROPERTY_NAME = "sys.system_server.start_count"
PROPERTY_VALUE = "1"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def kernel_state():
    result = {}
    for name, path in {
        "kptr_restrict": "/proc/sys/kernel/kptr_restrict",
        "process_context": "/proc/self/attr/current",
        "enforce": "/sys/fs/selinux/enforce",
    }.items():
        try:
            result[name] = Path(path).read_text().strip("\0\n")
        except OSError as error:
            result[name] = {"error": type(error).__name__}
    try:
        data = Path("/sys/fs/selinux/status").read_bytes()[:20]
        result["selinux_status"] = dict(zip(
            ("version", "sequence", "enforcing", "policy_load", "deny_unknown"),
            struct.unpack("=5I", data),
        ))
    except (OSError, struct.error) as error:
        result["selinux_status"] = {"error": type(error).__name__}
    result["property_service_class_exists"] = Path("/sys/fs/selinux/class/property_service").is_dir()
    return result


def property_request(path, name=PROPERTY_NAME, value=PROPERTY_VALUE):
    property_name, property_value = name, value
    name, value = name.encode(), value.encode()
    request = (struct.pack("<II", PROP_MSG_SETPROP2, len(name)) + name
               + struct.pack("<I", len(value)) + value)
    result = {"connected": False, "response_code": None, "property": property_name,
              "value": property_value, "readback_verified": False}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(path))
        result["connected"] = True
        try:
            client.sendall(request)
            response = bytearray()
            while len(response) < 4:
                block = client.recv(4 - len(response))
                if not block:
                    raise EOFError("property response ended before four bytes")
                response.extend(block)
            result["response_code"] = struct.unpack("<I", response)[0]
        except (OSError, EOFError) as error:
            result["error"] = type(error).__name__
    return result


def summarize(request, socket_created, exit_code):
    verified = (request.get("connected") is True and request.get("response_code") == 0
                and request.get("readback_exit") == 0
                and isinstance(request.get("value"), str)
                and request.get("readback_value") == request["value"]
                and request.get("readback_verified") is True)
    if verified:
        status = "INIT_PROPERTY_WRITE_READBACK_PASS_LIMITED"
    elif request.get("connected") and request.get("response_code") == PROP_ERROR_PERMISSION_DENIED:
        status = "INIT_PROPERTY_REQUEST_REJECTED_LIMITED"
    elif request.get("connected") and request.get("response_code") == 0:
        status = "INIT_PROPERTY_WRITE_ACK_ONLY_LIMITED"
    elif socket_created:
        status = "INIT_PROPERTY_SOCKET_ONLY_LIMITED"
    else:
        status = "INIT_PROPERTY_BLOCKED_LIMITED"
    return {"status": status, "init_launcher_exit": exit_code,
            "socket_created": socket_created, "request": request,
            "property_write_verified": verified, "system_services_ready": False,
            "game_context_initialized": False, "catalog_ready_pass": False}


def validate_paths(closure, vendor, launcher, output):
    if not closure.is_dir() or not vendor.is_dir() or not launcher.is_file():
        raise ValueError("missing closure, vendor inputs or launcher")
    if output.exists():
        raise ValueError("output already exists")
    if len(os.fsencode(output / "socket/property_service")) >= 104:
        raise ValueError("output path exceeds portable Unix socket address limit")
    if output == Path("/") or any(base == output or base in output.parents
                                  for base in (closure, vendor)):
        raise ValueError("output must be outside input directories")


def run(closure, vendor, launcher, output, expected_init_sha256, timeout=20,
        root_namespace=False, request_client=None, extra_environment=None):
    closure, vendor, launcher, output = (Path(p).resolve() for p in (closure, vendor, launcher, output))
    validate_paths(closure, vendor, launcher, output)
    init = closure / "system/bin/init"
    if sha256(init) != expected_init_sha256:
        raise ValueError("original init identity mismatch")
    if root_namespace and os.geteuid() != 0:
        raise PermissionError("root PID namespace trial requires root")
    version = vendor / "etc/selinux/plat_sepolicy_vers.txt"
    contexts = vendor / "etc/selinux/vendor_property_contexts"
    for path in (version, contexts):
        if not path.is_file():
            raise ValueError("missing original vendor SELinux input")
    inputs = {"init": init, "launcher": launcher,
              "platform_property_contexts": closure / "system/etc/selinux/plat_property_contexts"}
    inputs.update({"vendor/" + str(p.relative_to(vendor)): p
                   for p in vendor.rglob("*") if p.is_file()})
    if extra_environment:
        for key in ("NASHURA_GRAPHICS_INPUTS", "NASHURA_GRAPHICS_VENDOR_ROOT"):
            base = extra_environment.get(key)
            if not base:
                continue
            base = Path(base)
            if key == "NASHURA_GRAPHICS_INPUTS":
                paths = [base / "surfaceflinger", base / "gpuservice",
                         base / "libstatssocket.so", base / "libstatspull.so"]
            else:
                paths = ([base / "bin/hw/android.hardware.graphics.allocator@3.0-service",
                          base / "bin/hw/android.hardware.graphics.composer@2.3-service"]
                         + list((base / "lib64").glob("*.so"))
                         + list((base / "lib64/hw").glob("*.so")))
            inputs.update({key + "/" + str(p.relative_to(base)): p for p in paths if p.is_file()})
    # Protect earlier ART property evidence from accidental reuse or replacement.
    original_properties = closure / "dev/__properties__"
    inputs.update({"prior_properties/" + p.name: p for p in original_properties.glob("*") if p.is_file()})
    before = {key: sha256(path) for key, path in inputs.items()}
    kernel_before = kernel_state()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (output / "properties").mkdir(mode=0o711)
    (output / "socket").mkdir(mode=0o755)
    (output / "kmsg").touch(exist_ok=False)
    environment = dict(os.environ, **(extra_environment or {}))
    environment.update(NASHURA_BIND_SELINUXFS="1", NASHURA_INIT_STATE=str(output),
                       NASHURA_VENDOR_INPUTS=str(vendor))
    namespace = ["unshare", "-Ur", "-m"]
    if root_namespace:
        namespace = ["unshare", "-m", "--pid", "--fork", "--kill-child=SIGKILL",
                     "--mount-proc", "--cgroup", "--net", "--ipc"]
    command = (["strace", "-f", "-s", "256", "-o", str(output / "init.strace")]
               + namespace + ["/bin/sh", str(launcher), str(closure),
                              "/system/bin/init", "second_stage"])
    (output / "command.json").write_text(json.dumps({"argv": command, "environment": {
        k: environment[k] for k in ("NASHURA_BIND_SELINUXFS", "NASHURA_INIT_STATE", "NASHURA_VENDOR_INPUTS")}},
        indent=2) + "\n")
    request = {"connected": False, "response_code": None, "readback_verified": False}
    stop = threading.Event()

    def observe_socket():
        path = output / "socket/property_service"
        while not stop.is_set():
            try:
                observed = (request_client or property_request)(path)
            except OSError as error:
                request["last_connect_error"] = type(error).__name__
                stop.wait(0.001)
                continue
            except Exception as error:
                request["observer_error"] = type(error).__name__
                request["observer_error_message"] = str(error)
                break
            request.clear()
            request.update(observed)
            break

    watcher = threading.Thread(target=observe_socket, daemon=True)
    timed_out = False
    with (output / "stdout").open("xb") as stdout, (output / "stderr").open("xb") as stderr:
        process = subprocess.Popen(command, env=environment, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
        watcher.start()
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                exit_code = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                exit_code = process.wait(timeout=3)
        finally:
            stop.set()
            # A bounded custom client may still be collecting readback after init exits.
            watcher.join(timeout=max(3, timeout))
    if watcher.is_alive():
        raise RuntimeError("property observer did not finish")
    (output / "exit").write_text(str(exit_code) + "\n")
    path = output / "socket/property_service"
    socket_created = path.exists() and stat.S_ISSOCK(path.stat().st_mode)
    result = summarize(request, socket_created, exit_code)
    after = {key: sha256(path) for key, path in inputs.items()}
    kernel_after = kernel_state()
    result.update(schema_version=1, timed_out=timed_out, inputs_before=before, inputs_after=after,
                  inputs_unchanged=(before == after), kernel_before=kernel_before,
                  kernel_after=kernel_after, kernel_state_unchanged=(kernel_before == kernel_after),
                  vendor_selinux_version=version.read_text().strip(),
                  property_files={p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)}
                                  for p in (output / "properties").iterdir() if p.is_file()},
                  outputs={name: sha256(output / name) for name in
                           ("stdout", "stderr", "kmsg", "init.strace", "exit", "command.json")})
    with (output / "result.json").open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("closure", "vendor", "launcher", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-init-sha256", required=True)
    args = parser.parse_args()
    result = run(args.closure, args.vendor, args.launcher, args.output, args.expected_init_sha256)
    print(json.dumps({key: result[key] for key in
                      ("status", "init_launcher_exit", "request", "inputs_unchanged", "kernel_state_unchanged")}))
    # Exit 0 means a complete observation, not a passing Android boot or property write.
    return 0 if result["inputs_unchanged"] and result["kernel_state_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
