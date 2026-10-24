from copy import deepcopy
import re


SCOPE = "native-payload+global-mt2504.v1"
DIGEST_FIELDS = ("scope", "tick", "sha256", "nativeSha256")
HEX_SHA = re.compile(r"[0-9a-f]{64}")


def checked_digest(value):
    if (value.get("scope") != SCOPE or type(value.get("tick")) is not int
            or value["tick"] < 0 or any(not isinstance(value.get(name), str)
                or not HEX_SHA.fullmatch(value[name]) for name in ("sha256", "nativeSha256"))):
        raise ValueError("search snapshot digest scope or encoding differs")
    return {name: value[name] for name in DIGEST_FIELDS}


def positive_integer(value):
    return type(value) is int and value > 0


class SnapshotLedger:
    def __init__(self):
        self.active = {}
        self.records = []

    def create(self, receipt, digest):
        digest = checked_digest(digest)
        if (receipt.get("ok") is not True or not positive_integer(receipt.get("handle"))
                or receipt["handle"] in self.active or receipt.get("scope") != "process"
                or receipt.get("tick") != digest["tick"]
                or any(not positive_integer(receipt.get(name))
                    for name in ("generation", "configRevision", "stateEpoch"))):
            raise ValueError("search snapshot creation identity differs")
        self.active[receipt["handle"]] = dict(receipt=deepcopy(receipt), digest=digest,
            last_epoch=receipt["stateEpoch"])
        self.records.append(dict(operation="create", receipt=deepcopy(receipt), digest=digest))

    def restore(self, handle, receipt, digest):
        digest = checked_digest(digest)
        original = self.active.get(handle)
        if (not positive_integer(handle) or original is None
                or receipt.get("ok") is not True or receipt.get("restored") is not True
                or receipt.get("handle") != handle or digest != original["digest"]
                or receipt.get("tick") != digest["tick"]
                or any(not positive_integer(receipt.get(name)) for name in ("handle", "generation", "configRevision"))
                or any(receipt.get(name) != original["receipt"][name]
                    for name in ("generation", "configRevision"))
                or not positive_integer(receipt.get("stateEpoch"))
                or receipt["stateEpoch"] <= original["last_epoch"]):
            raise ValueError("search snapshot restoration identity or complete digest differs")
        original["last_epoch"] = receipt["stateEpoch"]
        self.records.append(dict(operation="restore", handle=handle,
            receipt=deepcopy(receipt), digest=digest))

    def release(self, handle, receipt):
        original = self.active.get(handle)
        if (not positive_integer(handle) or original is None
                or receipt.get("ok") is not True or receipt.get("released") is not True
                or receipt.get("handle") != handle
                or not positive_integer(receipt.get("handle")) or not positive_integer(receipt.get("generation"))
                or receipt.get("generation") != original["receipt"]["generation"]):
            raise ValueError("search snapshot release identity differs")
        self.records.append(dict(operation="release", handle=handle, receipt=deepcopy(receipt)))
        del self.active[handle]

    def summary(self):
        if self.active:
            raise ValueError("search snapshot handles remain active")
        counts = {name: sum(row["operation"] == name for row in self.records)
            for name in ("create", "restore", "release")}
        if not counts["create"] or counts["create"] != counts["release"] or not counts["restore"]:
            raise ValueError("search snapshot audit lacks complete lifecycle")
        return dict(counts=counts, outstanding=0, scope=SCOPE)


def validate_records(records):
    ledger = SnapshotLedger()
    for record in records:
        operation = record.get("operation")
        expected = ({"operation", "receipt", "digest"} if operation == "create" else
            {"operation", "handle", "receipt", "digest"} if operation == "restore" else
            {"operation", "handle", "receipt"})
        if set(record) != expected:
            raise ValueError("search snapshot audit record shape differs")
        if operation == "create":
            ledger.create(record["receipt"], record["digest"])
        elif operation == "restore":
            ledger.restore(record["handle"], record["receipt"], record["digest"])
        elif operation == "release":
            ledger.release(record["handle"], record["receipt"])
        else:
            raise ValueError("search snapshot audit operation differs")
    return ledger.summary()
