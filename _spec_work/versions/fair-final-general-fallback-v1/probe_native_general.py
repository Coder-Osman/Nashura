import hashlib
import json
from pathlib import Path
import sys

from general_provider import GeneralHistorySearchProvider
from native_runner.training.fair_final.episode import atomic_json
from tools import probe_fair_final_refill_provider as refill_probe


class NativeGeneralProvider(GeneralHistorySearchProvider):
    @property
    def cursor(self):
        return self.local.cursor

    def build(self, information, *, seed):
        world = super().build(information, seed=seed)
        if world.manifest["mode"] != "local":
            world.close()
            raise ValueError("registered native probe covers local general-provider roots only")
        world.root = world.world.root
        world.rollout = world.world.rollout
        world.local_profile = world.world.local_profile
        return world


def main():
    refill_probe.RefillLocalSearchProvider = NativeGeneralProvider
    refill_probe.main()
    output = Path(sys.argv[sys.argv.index("--output") + 1])
    report = json.loads(output.read_text())
    report.update(schema="fair-final-general-provider-native-local.v1",
        general_provider_sha256=hashlib.sha256(
            Path(__file__).with_name("general_provider.py").read_bytes()).hexdigest(),
        general_probe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        native_pending_verified=False, native_fallback_verified=False, gates_closed=False)
    atomic_json(output, report)


if __name__ == "__main__":
    main()
