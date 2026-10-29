"""Bounded root init/property trial in an ARM64 Linux guest with a loaded policy.

The default prints a validated plan. --apply reuses the pinned permissive policy;
it never loads policy. Use a disposable extracted closure, not a running root.
All three init sysctls are restored even when observation fails.
"""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from probe_init_property_service import (
    PROPERTY_NAME, kernel_state, property_request, run, sha256, validate_paths,
)
from apex_loop_devices import ApexLoopDevices

KNOBS = ("kernel/kptr_restrict", "vm/mmap_rnd_bits", "vm/mmap_rnd_compat_bits")
SYSCTL_ROOT = Path("/proc/sys")
POLICY = Path("/sys/fs/selinux/policy")


def write_json(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def validate(settings, output):
    closure, vendor, launcher, tools = (Path(settings[k]).resolve()
                                        for k in ("closure", "vendor", "launcher", "tools"))
    names = ["closure", "vendor", "launcher", "tools", "backup"]
    names += [name for name in ("graphics_inputs", "vendor_root") if settings.get(name)]
    for name in names:
        if settings[name] != str(Path(settings[name]).resolve()):
            raise ValueError("absolute canonical input paths are required")
    validate_paths(closure, vendor, launcher, output / "runtime")
    if output.exists() or output == tools or tools in output.parents:
        raise ValueError("new output outside inputs is required")
    required = [closure / "system/bin/init", vendor / "etc/selinux/precompiled_sepolicy",
                closure / "system/build.prop", closure / "system/etc/prop.default",
                launcher, tools / "toolbox", tools / "nashura_namespace_exec", Path(settings["backup"])]
    if settings.get("services"):
        required += [Path(settings["linker_config"]), tools / "art-system-services.dex",
                     tools / "hwservicemanager",
                     tools / "android.system.suspend@1.0-service",
                     closure / "apex/com.android.art/bin/servicemanager-probe",
                     closure / "apex/com.android.art/bin/app_process64-holder-v3",
                     closure / "data/local/tmp/services-original.jar"]
    if settings.get("graphics_inputs"):
        graphics = Path(settings["graphics_inputs"])
        statsd_libs = closure / "system/apex/com.android.os.statsd/lib64"
        required += [graphics / "surfaceflinger", graphics / "gpuservice",
                     graphics / "libstatssocket.so", graphics / "libstatspull.so"]
        required += [statsd_libs / "libstatssocket.so", statsd_libs / "libstatspull.so"]
        vendor_root = Path(settings["vendor_root"])
        required += [vendor_root / "bin/hw/android.hardware.graphics.allocator@3.0-service",
                     vendor_root / "bin/hw/android.hardware.graphics.composer@2.3-service"]
    if settings.get("apex_packages"):
        packages = Path(settings["apex_packages"])
        if str(packages.resolve()) != str(packages) or not packages.is_dir():
            raise ValueError("canonical APEX package directory is required")
        if not (packages / "com.android.runtime.apex").is_file():
            raise ValueError("original runtime APEX is required")
        required += list(packages.glob("*.apex"))
    identities = settings["identities"]
    for path in required:
        if str(path) not in identities:
            raise ValueError("missing required input identity: " + str(path))
    for name, expected in identities.items():
        if sha256(name) != expected:
            raise ValueError("input identity mismatch: " + name)
    if settings.get("graphics_inputs"):
        statsd_libs = closure / "system/apex/com.android.os.statsd/lib64"
        for library in ("libstatssocket.so", "libstatspull.so"):
            if sha256(Path(settings["graphics_inputs"]) / library) != sha256(statsd_libs / library):
                raise ValueError("graphics library must match original statsd APEX: " + library)
    before = kernel_state()
    if before.get("enforce") != "0" or before.get("selinux_status", {}).get("policy_load") != 1:
        raise ValueError("one preloaded permissive original policy is required")
    if sha256(POLICY) != settings["loaded_policy_sha256"]:
        raise ValueError("loaded policy identity mismatch")
    return {"kernel_before": before, "sysctls_before": {
        name: (SYSCTL_ROOT / name).read_text().strip() for name in KNOBS},
        "identities": identities, "timeout_seconds": 20,
        "policy_action": "reuse loaded policy; no policy write",
        "apex_packages": settings.get("apex_packages"),
        "loop_action": ("lease idle loops, allocate new loops, verify backing device/inode on cleanup; "
                        "preserve occupied loops and dm mappings" if settings.get("apex_packages") else "none"),
        "sysctl_action": "restore recorded values in finally",
        "system_services_ready": False, "catalog_ready_pass": False}


def namespace(settings):
    return ["unshare", "-m", "--pid", "--fork", "--kill-child=SIGKILL",
            "--mount-proc", "--cgroup", "--net", "--ipc", "/bin/sh",
            settings["launcher"], settings["closure"]]


def services(settings, output, environment):
    """Observe real services; exit 0 alone is never a readiness assertion."""
    boot = ["/apex/com.android.art/javalib/" + name + ".jar" for name in
            ("core-oj", "core-libart", "core-icu4j", "okhttp", "bouncycastle", "apache-xml")]
    boot += ["/apex/com.android.conscrypt/javalib/conscrypt.jar", "/data/local/tmp/framework-sdk.jar",
             "/apex/com.android.os.statsd/javalib/framework-statsd.jar",
             "/data/local/tmp/framework-tethering.jar", "/data/local/tmp/framework-wifi.jar"]
    env = dict(environment, NASHURA_ART_LINKER_CONFIG=settings["linker_config"],
               NASHURA_EXEC_UID=str(settings["service_uid"]), NASHURA_EXEC_GID=str(settings["service_gid"]),
               NASHURA_RUN_HWSERVICEMANAGER="1",
               NASHURA_RUN_SUSPEND_HAL="1",
               CLASSPATH="/nashura-tools/art-system-services.dex:/data/local/tmp/services-original.jar:"
                         "/data/local/tmp/current-game-classes.dex")
    if settings.get("graphics_inputs"):
        env["NASHURA_GRAPHICS_INPUTS"] = settings["graphics_inputs"]
        env["NASHURA_GRAPHICS_VENDOR_ROOT"] = settings["vendor_root"]
    prefix = namespace(settings)
    commands = {
        "service": ["strace", "-f", "-s", "256", "-o", str(output / "service.strace")]
                   + prefix + ["/apex/com.android.art/bin/servicemanager-probe"],
        "hwservicemanager": ["strace", "-f", "-s", "256", "-o", str(output / "hwservicemanager.strace")]
                             + prefix + ["/system/bin/hwservicemanager"],
        "suspend_hal": ["strace", "-f", "-s", "256", "-o", str(output / "suspend-hal.strace")]
                        + prefix + ["/system/bin/hw/android.system.suspend@1.0-service"],
        "probe": ["prlimit", "--nice=40:40", "--", "strace", "-f", "-s", "256", "-o",
                  str(output / "probe.strace")] + prefix
                 + ["/apex/com.android.art/bin/app_process64-holder-v3", "-Xbootclasspath:" + ":".join(boot),
                    "-Djava.library.path=/system/lib64:/data/local/tmp/libg-input", "/system/bin",
                    "ArtSystemServicesProbe", "display" if settings.get("graphics_inputs") else "services"],
    }
    if settings.get("graphics_inputs"):
        commands["allocator"] = (["strace", "-f", "-s", "256", "-o", str(output / "allocator.strace")]
                                 + prefix + ["/vendor/bin/hw/android.hardware.graphics.allocator@3.0-service"])
        commands["composer"] = (["strace", "-f", "-s", "256", "-o", str(output / "composer.strace")]
                                + prefix + ["/vendor/bin/hw/android.hardware.graphics.composer@2.3-service"])
        commands["surfaceflinger"] = ["strace", "-f", "-s", "256", "-o",
                                      str(output / "surfaceflinger.strace")] + prefix + ["/system/bin/surfaceflinger"]
        commands["gpuservice"] = (["strace", "-f", "-s", "256", "-o", str(output / "gpuservice.strace")]
                                  + prefix + ["/system/bin/gpuservice"])
    write_json(output / "services-command.json", commands)
    result = {"system_services_ready": False}

    def stop(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        return process.wait(timeout=2)

    processes, streams = {}, {}

    def start(name, delay):
        stdout = (output / (name + ".stdout")).open("xb")
        stderr = (output / (name + ".stderr")).open("xb")
        streams[name] = (stdout, stderr)
        process_env = env
        if settings.get("graphics_inputs") and name in {
                "allocator", "composer", "surfaceflinger", "gpuservice"}:
            process_env = dict(env, NASHURA_EXEC_GROUPS="1003,1030,1073")
        process = subprocess.Popen(commands[name], env=process_env, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
        processes[name] = process
        time.sleep(delay)
        result[name + "_alive_before"] = process.poll() is None
        return process

    try:
        service = start("service", .4)
        start("hwservicemanager", .2)
        if settings.get("graphics_inputs"):
            start("allocator", .25)
            start("composer", .25)
        start("suspend_hal", .3)
        if settings.get("graphics_inputs"):
            start("surfaceflinger", .35)
            start("gpuservice", .2)
        probe = start("probe", 0)
        try:
            result["probe_exit"] = probe.wait(timeout=9)
        finally:
            result["probe_exit_after_cleanup"] = stop(probe)
        probe_lines = set((output / "probe.stdout").read_text(errors="replace").splitlines())
        required = {
            "ART_ANDROID_SERVERS_LOADED=true", "ART_SYSTEM_CONTEXT_PRESENT=true",
            "ART_POWER_SERVICE_STARTED=true", "ART_THERMAL_SERVICE_STARTED=true",
            "ART_POWER_BINDER_PRESENT=true", "ART_THERMAL_BINDER_PRESENT=true",
            "ART_POWER_SERVICE_LISTED=true", "ART_POWER_FETCHER_PRESENT=true",
            "ART_DIRECT_POWER_FETCHER_PRESENT=true", "ART_POWER_FACTORY_RESULT_PRESENT=true",
            "ART_CONTEXT_POWER_PRESENT=true", "ART_POWER_INTERNAL_PRESENT=true",
            "ART_POWER_THERMAL_FETCHER_PROBE_RETURNED_LIMITED",
        }
        result["power_thermal_fetcher_probe_pass"] = (result.get("probe_exit") == 0 and
                                                          required <= probe_lines)
        result["power_thermal_fetcher_missing_observations"] = sorted(required - probe_lines)
        display_required = {"ART_DISPLAY_SERVICE_STARTED=true", "ART_DISPLAY_BINDER_PRESENT=true"}
        if settings.get("graphics_inputs"):
            result["display_service_probe_pass"] = (result.get("probe_exit") == 0 and
                                                      display_required <= probe_lines)
            result["display_service_missing_observations"] = sorted(display_required - probe_lines)
        result["service_alive_after"] = service.poll() is None
    finally:
        for name in reversed(list(processes)):
            result[name + "_exit_after_cleanup"] = stop(processes[name])
        for stdout, stderr in streams.values():
            stdout.close()
            stderr.close()
        write_json(output / "services-result.json", result)
    return result


def trial(settings, output, apply=False):
    output = Path(output).resolve()
    plan = validate(settings, output)
    if not apply:
        return plan
    if os.geteuid() != 0:
        raise PermissionError("--apply requires guest root")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(output / "plan.json", plan)
    environment = dict(os.environ, NASHURA_BIND_SELINUXFS="1", NASHURA_ROOT_INIT_TRIAL="1",
                       NASHURA_TOOLS_INPUTS=settings["tools"], NASHURA_VENDOR_INPUTS=settings["vendor"],
                       NASHURA_INIT_STATE=str(output / "runtime"))
    requests = []

    def request_and_readback(socket_path):
        for value in ("1", "2", "3"):
            try:
                response = property_request(socket_path, value=value)
            except OSError as error:
                if not requests:
                    raise
                response = {"connected": False, "response_code": None, "value": value,
                            "readback_verified": False, "error": type(error).__name__}
            if response.get("response_code") == 0:
                command = ["unshare", "-m", "/bin/sh", settings["launcher"], settings["closure"],
                           "/nashura-tools/toolbox", "getprop", PROPERTY_NAME]
                readback = subprocess.run(command, env=environment, capture_output=True, timeout=2)
                (output / ("getprop-" + value + ".stdout")).write_bytes(readback.stdout)
                (output / ("getprop-" + value + ".stderr")).write_bytes(readback.stderr)
                response.update(readback_exit=readback.returncode,
                                readback_value=readback.stdout.decode(errors="replace").strip())
                response["readback_verified"] = (readback.returncode == 0 and response["readback_value"] == value)
            requests.append(response)
            write_json(output / ("request-" + value + ".json"), response)
            if value == "1" and response.get("readback_verified") and settings.get("services"):
                try:
                    services(settings, output, environment)
                except Exception as error:
                    write_json(output / "services-error.json", {"type": type(error).__name__, "message": str(error)})
        return requests[-1]

    observed, failure, restored = None, None, {}
    try:
        loops = (ApexLoopDevices(output / "apex-loops", settings["apex_packages"])
                 if settings.get("apex_packages") else nullcontext())
        with loops as devices:
            if devices:
                environment.update(NASHURA_APEX_PACKAGES=settings["apex_packages"],
                                   NASHURA_APEX_LOOP_DEVICES=str(devices))
            observed = run(settings["closure"], settings["vendor"], settings["launcher"], output / "runtime",
                           settings["identities"][str(Path(settings["closure"]) / "system/bin/init")],
                           root_namespace=True, request_client=request_and_readback, extra_environment=environment)
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
    finally:
        for name, value in plan["sysctls_before"].items():
            try:
                path = SYSCTL_ROOT / name
                path.write_text(value + "\n")
                restored[name] = path.read_text().strip() == value
            except OSError as error:
                restored[name] = {"error": type(error).__name__, "message": str(error)}
        after = {name: sha256(name) for name in settings["identities"]}
        verified = (failure is None and after == settings["identities"]
                    and all(v is True for v in restored.values())
                    and len(requests) == 3 and all(r.get("readback_verified") is True for r in requests))
        result = {"status": "INIT_PROPERTY_CONTINUITY_PASS_LIMITED" if verified else "INIT_ROOT_NOT_VERIFIED",
                  "requests": requests, "observation": observed, "failure": failure,
                  "inputs_unchanged": after == settings["identities"], "inputs_after": after,
                  "sysctl_restore_results": restored, "sysctls_restored": all(v is True for v in restored.values()),
                  "kernel_after": kernel_state(), "policy_remains_loaded_until_guest_reboot": True,
                  "system_services_ready": False, "game_context_initialized": False, "catalog_ready_pass": False}
        write_json(output / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = trial(json.loads(args.settings.read_text()), args.output, args.apply)
    print(json.dumps(result, indent=2))
    if args.apply and (result["failure"] or not result["sysctls_restored"] or not result["inputs_unchanged"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
