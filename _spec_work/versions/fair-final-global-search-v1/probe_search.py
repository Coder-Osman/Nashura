import hashlib
import json
from pathlib import Path
import sys

from native_runner.training.fair_final.episode import atomic_json
from native_runner.training.fair_search.transport import CompactSearchNative
from tools import probe_fair_final_refill_provider as refill_probe
from general_provider import GeneralHistorySearchProvider
from probe_native_general import NativeGeneralProvider
from snapshot_audit import SnapshotLedger, checked_digest


TRANSPORTS = []
SEARCH_SPANS = []


class AuditedSearch(refill_probe.CompletedBoundarySearch):
    def run(self, information, **kwargs):
        starts = [len(native.snapshot_ledger.records) for native in TRANSPORTS]
        outcome = super().run(information, **kwargs)
        spans = []
        for index, native in enumerate(TRANSPORTS):
            start = starts[index] if index < len(starts) else 0
            end = len(native.snapshot_ledger.records)
            if end > start:
                spans.append(dict(transport=index, start=start, end=end))
        SEARCH_SPANS.append(dict(fair_sha=information.sha256, tick=information.tick, spans=spans))
        return outcome


class AuditedNative(CompactSearchNative):
    def __init__(self, *, port):
        self.snapshot_ledger = SnapshotLedger()
        self.audit_port = port
        super().__init__(port=port, event_deltas=True)
        TRANSPORTS.append(self)

    def _request(self, command, **kwargs):
        control = super()._request
        if command == "snapshot-create":
            digest = checked_digest(control("state-digest"))
            receipt = control(command, **kwargs)
            self.snapshot_ledger.create(receipt, digest)
            return receipt
        if command.startswith("restore "):
            handle = int(command.split()[1])
            receipt = control(command, **kwargs)
            self.snapshot_ledger.restore(handle, receipt, control("state-digest"))
            return receipt
        if command.startswith("release-snapshot "):
            handle = int(command.split()[1])
            receipt = control(command, **kwargs)
            self.snapshot_ledger.release(handle, receipt)
            return receipt
        result = control(command, **kwargs)
        if command == "state-digest":
            checked_digest(result)
        return result


def main():
    if TRANSPORTS or SEARCH_SPANS:
        raise ValueError("global search probe must start in a new process")
    output = Path(sys.argv[sys.argv.index("--output") + 1])
    refill_probe.create_search_native = lambda port, **unused: AuditedNative(port=port)
    refill_probe.RefillLocalSearchProvider = NativeGeneralProvider
    refill_probe.CompletedBoundarySearch = AuditedSearch
    refill_probe.main()
    audits = [dict(port=native.audit_port, records=native.snapshot_ledger.records,
        summary=native.snapshot_ledger.summary()) for native in TRANSPORTS]
    report = json.loads(output.read_text())
    report.update(schema="FairFinalGlobalSnapshotSearchProbe/v1", snapshot_audits=audits,
        snapshot_audit_spans=SEARCH_SPANS,
        engine_sha256=json.loads((Path(__file__).parent.parent / "settings.json").read_text())["engine_sha256"],
        general_provider_sha256=hashlib.sha256(
            Path(sys.modules[GeneralHistorySearchProvider.__module__].__file__).read_bytes()).hexdigest(),
        native_pending_verified=False, native_fallback_verified=False, gates_closed=False)
    atomic_json(output, report)


if __name__ == "__main__":
    main()
