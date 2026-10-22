import hashlib
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
PROBE = PROJECT / "research/standalone-resident-p18-stepatomic/attestation-history/libcrprobe.so"
PROBE_SHA256 = "0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807"


def install():
    if hashlib.sha256(PROBE.read_bytes()).hexdigest() != PROBE_SHA256:
        raise RuntimeError("conditional draw native artifact identity changed")
    configured = os.environ.get("CR_PROBE")
    if configured and Path(configured).resolve() != PROBE.resolve():
        raise RuntimeError("configured probe differs from isolated conditional draw artifact")
    os.environ["CR_PROBE"] = str(PROBE)
    return PROBE
