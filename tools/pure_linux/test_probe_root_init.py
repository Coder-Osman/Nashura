import json
from pathlib import Path

import pytest

import probe_root_init as probe


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Use short relative test output after chdir to avoid macOS sockaddr_un limits.
    closure, vendor, tools = (tmp_path / n for n in ("closure", "vendor", "tools"))
    launcher, backup, policy = (tmp_path / n for n in ("launcher.sh", "backup.tar.gz", "policy"))
    paths = [closure / "system/bin/init", closure / "system/build.prop", closure / "system/etc/prop.default",
             vendor / "etc/selinux/precompiled_sepolicy", tools / "toolbox", tools / "nashura_namespace_exec",
             launcher, backup, policy]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic unit input")
    sysctls = tmp_path / "sysctl"
    for name in probe.KNOBS:
        path = sysctls / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("17\n")
    monkeypatch.setattr(probe, "SYSCTL_ROOT", sysctls)
    monkeypatch.setattr(probe, "POLICY", policy)
    monkeypatch.setattr(probe, "kernel_state", lambda: {"enforce": "0", "selinux_status": {"policy_load": 1}})
    # validate_paths alone is exercised with real socket limits in its own tests.
    monkeypatch.setattr(probe, "validate_paths", lambda *args: None)
    result = {k: str(v) for k, v in dict(closure=closure, vendor=vendor, tools=tools,
                                       launcher=launcher, backup=backup).items()}
    result.update(identities={str(p): probe.sha256(p) for p in paths[:-1]}, loaded_policy_sha256=probe.sha256(policy))
    return result


def test_plan_is_read_only_and_pins_loaded_policy(settings):
    plan = probe.trial(settings, Path("output"))
    assert not Path("output").exists()
    assert plan["sysctls_before"] == dict.fromkeys(probe.KNOBS, "17")
    settings["loaded_policy_sha256"] = "wrong"
    with pytest.raises(ValueError, match="loaded policy identity"):
        probe.trial(settings, Path("output"))


def test_changed_or_unpinned_inputs_rejected_before_output(settings):
    Path(settings["backup"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="input identity"):
        probe.trial(settings, Path("output"))
    del settings["identities"][settings["backup"]]
    with pytest.raises(ValueError, match="missing required input identity"):
        probe.trial(settings, Path("output"))
    assert not Path("output").exists()


def test_apply_requires_root_without_creating_output(settings, monkeypatch):
    monkeypatch.setattr(probe.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError):
        probe.trial(settings, Path("output"), apply=True)
    assert not Path("output").exists()


def test_original_apex_inputs_require_pinned_identity(settings):
    packages = Path(settings["closure"]).parent / "packages"
    packages.mkdir()
    runtime = packages / "com.android.runtime.apex"
    runtime.write_bytes(b"synthetic test package")
    settings["apex_packages"] = str(packages)
    with pytest.raises(ValueError, match="missing required input identity"):
        probe.trial(settings, Path("output"))
    settings["identities"][str(runtime)] = probe.sha256(runtime)
    settings.update(services=True, linker_config=str(Path(settings["closure"]).parent / "ld.config"))
    service_inputs = [Path(settings["linker_config"]), Path(settings["tools"]) / "art-system-services.dex",
                      Path(settings["tools"]) / "hwservicemanager",
                      Path(settings["tools"]) / "android.system.suspend@1.0-service",
                      Path(settings["closure"]) / "apex/com.android.art/bin/servicemanager-probe",
                      Path(settings["closure"]) / "apex/com.android.art/bin/app_process64-holder-v3",
                      Path(settings["closure"]) / "data/local/tmp/services-original.jar"]
    for path in service_inputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic service input")
    settings["identities"].update({str(path): probe.sha256(path) for path in service_inputs
                                   if path.name != "hwservicemanager"})
    with pytest.raises(ValueError, match="missing required input identity"):
        probe.trial(settings, Path("output"))
    manager = Path(settings["tools"]) / "hwservicemanager"
    settings["identities"][str(manager)] = probe.sha256(manager)
    assert probe.trial(settings, Path("output"))["system_services_ready"] is False


def test_services_launch_both_binder_managers_before_java(tmp_path, monkeypatch):
    settings = {"launcher": "/probe/launcher.sh", "closure": "/probe/closure",
                "linker_config": "/probe/ld.config", "service_uid": 1000, "service_gid": 1000,
                "tools": "/probe/tools"}
    calls = []

    class Process:
        def __init__(self, command):
            self.command = command
            self.pid = len(calls) + 100
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

    def popen(command, **_):
        calls.append(command)
        return Process(command)

    monkeypatch.setattr(probe.subprocess, "Popen", popen)
    monkeypatch.setattr(probe.os, "killpg", lambda *_: None)
    monkeypatch.setattr(probe.time, "sleep", lambda *_: None)
    result = probe.services(settings, tmp_path, {})
    assert ["/system/bin/hwservicemanager"] == calls[1][-1:]
    assert calls[0][-1:] == ["/apex/com.android.art/bin/servicemanager-probe"]
    assert calls[2][-1:] == ["/system/bin/hw/android.system.suspend@1.0-service"]
    assert calls[3][-3:] == ["/system/bin", "ArtSystemServicesProbe", "display"]
    assert result["hwservicemanager_alive_before"] is True
    assert result["system_services_ready"] is False
def test_failed_observation_still_restores_every_sysctl(settings, monkeypatch):
    monkeypatch.setattr(probe.os, "geteuid", lambda: 0)

    def fail(*args, **kwargs):
        for name in probe.KNOBS:
            (probe.SYSCTL_ROOT / name).write_text("99\n")
        raise RuntimeError("controlled observation failure")

    monkeypatch.setattr(probe, "run", fail)
    result = probe.trial(settings, Path("output"), apply=True)
    assert result["sysctls_restored"] is True
    assert result["status"] == "INIT_ROOT_NOT_VERIFIED"
    assert result["failure"]["type"] == "RuntimeError"
    assert result["catalog_ready_pass"] is False
    for name in probe.KNOBS:
        assert (probe.SYSCTL_ROOT / name).read_text() == "17\n"
    assert json.loads(Path("output/result.json").read_text())["sysctls_restored"] is True
