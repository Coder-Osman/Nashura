import json
from pathlib import Path

from probe_trace import TraceAudit
from plugin_protocol import sha


class PluginAudit(TraceAudit):
    def _verify_process(self):
        process = Path("/proc") / str(self.pid)
        current = json.loads((self.instance / "process.json").read_text())
        settings_path = self.instance.parent / "plugin-settings.json"
        settings = json.loads(settings_path.read_text())
        qemu = settings["qemu"] if settings["variant"] == "baseline" else settings["plugin_qemu"]
        expected = settings["qemu_sha256"] if settings["variant"] == "baseline" else settings["plugin_qemu_sha256"]
        arguments = (process / "cmdline").read_bytes().split(b"\0")
        start_ticks = int((process / "stat").read_text().rsplit(")", 1)[1].split()[19])
        attestation = current.get("random_plugin_trace", {})
        if (current != self.record or current["phase"] != "ready" or current["port"] != self.port
                or start_ticks != int(current["child_start_ticks"])
                or (process / "cwd").resolve() != self.instance
                or arguments[0].decode() != qemu or sha(process / "exe") != expected
                or attestation.get("qemu_sha256") != expected
                or attestation.get("settings_sha256") != sha(settings_path)):
            raise ValueError("plugin native audit executable or ownership identity differs")
        return process
