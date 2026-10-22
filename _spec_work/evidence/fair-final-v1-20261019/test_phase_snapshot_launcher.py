import hashlib
import sys
from types import SimpleNamespace

import pytest

import phase_snapshot_launcher as launcher


def configure(monkeypatch, tmp_path, *, command_variant="normal", digest=None):
    source = tmp_path/"project/research/standalone-resident-p18-stepatomic/service.py"
    source.parent.mkdir(parents=True)
    source.write_text("baseline service fixture")
    probe = tmp_path/"experiment/libcrprobe.so"
    probe.parent.mkdir()
    probe.write_bytes(b"isolated phase engine fixture")
    baseline = str(source.parent/"libresident.so")
    arguments = ["qemu", "-E", "LD_LIBRARY_PATH=/baseline:/resources", "AssetProbe", baseline, "29660"]
    if command_variant == "missing-baseline":
        arguments.remove(baseline)
    if command_variant == "duplicate-baseline":
        arguments.append(baseline)
    if command_variant == "missing-path":
        arguments.remove("LD_LIBRARY_PATH=/baseline:/resources")
    if command_variant == "duplicate-path":
        arguments.append("LD_LIBRARY_PATH=/other")
    observed = {}

    def command(port, instance):
        observed["verified_baseline"] = True
        return arguments.copy(), dict(outputs={"libresident.so":"baseline-sha"})

    service = SimpleNamespace(ROOT=source.parent, command=command)

    def serve(port, instance):
        observed["port"] = port
        observed["instance"] = str(instance)
        observed["argv"], observed["identity"] = service.command(port, instance)

    service.serve = serve
    monkeypatch.setattr(launcher.importlib.util, "spec_from_file_location",
        lambda *unused:SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module:None)))
    monkeypatch.setattr(launcher.importlib.util, "module_from_spec", lambda unused:service)
    monkeypatch.setattr(sys, "argv", ["launcher", "--project", str(tmp_path/"project"),
        "--port", "29660", "--instance", str(tmp_path/"instance"), "--probe", str(probe),
        "--engine-sha256", digest or hashlib.sha256(probe.read_bytes()).hexdigest()])
    return observed, probe


def test_launcher_selects_isolated_binary_and_preserves_baseline_verification(monkeypatch, tmp_path):
    observed, probe = configure(monkeypatch, tmp_path)
    launcher.main()
    assert observed["verified_baseline"] is True
    assert observed["argv"].count(str(probe)) == 1
    assert "LD_LIBRARY_PATH="+str(probe.parent)+":/baseline:/resources" in observed["argv"]
    assert observed["identity"]["outputs"] == {"libresident.so":"baseline-sha"}
    assert observed["identity"]["phase_snapshot_engine"]["sha256"] == hashlib.sha256(probe.read_bytes()).hexdigest()


@pytest.mark.parametrize("variant", ["missing-baseline", "duplicate-baseline", "missing-path", "duplicate-path"])
def test_launcher_rejects_changed_service_contract(monkeypatch, tmp_path, variant):
    configure(monkeypatch, tmp_path, command_variant=variant)
    with pytest.raises(ValueError, match="baseline"):
        launcher.main()


def test_launcher_rejects_changed_binary(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path, digest="0"*64)
    with pytest.raises(ValueError, match="identity changed"):
        launcher.main()
