from types import SimpleNamespace

import pytest

import fleet_preflight as preflight


def complete_report():
    config = {"fleet": {"34": 2, "37": 1}}
    expected = {"config.json": "config-sha"}
    hosts = {}
    for host, required in config["fleet"].items():
        hosts[host] = {
            "task_present": True,
            "resident_present": True,
            "disk_free_gib": 30,
            "memory_available_gib": 40,
            "ready_ports": list(range(29920, 29920 + required)),
            "ready_task_engines": required,
            "runtime_hashes": expected.copy(),
        }
    return {"checked_at": 1000, "hosts": hosts}, config, expected


def test_complete_fleet_passes():
    report, config, expected = complete_report()
    assert preflight.readiness_blockers(report, config, expected, now=1001) == []


@pytest.mark.parametrize("checked_at", [0, 699, 1002])
def test_stale_or_future_report_is_blocked(checked_at):
    report, config, expected = complete_report()
    report["checked_at"] = checked_at
    assert "not fresh" in preflight.readiness_blockers(report, config, expected, now=1001)[0]


@pytest.mark.parametrize("field, value, message", [
    ("disk_free_gib", 19.9, "disk_free_gib"),
    ("memory_available_gib", 31.9, "memory_available_gib"),
    ("stop_markers", ["STOP"], "stop marker"),
    ("runtime_hashes", {}, "hash mismatch"),
    ("ready_ports", [29920, 29922], "respond ready"),
    ("ready_task_engines", 1, "respond ready"),
    ("error", "host unreachable", "host unreachable"),
])
def test_fleet_faults_are_blocked(field, value, message):
    report, config, expected = complete_report()
    report["hosts"]["34"][field] = value
    assert any(message in error for error in preflight.readiness_blockers(report, config, expected, now=1001))


def engine_probe(status=None, *, child_alive=True, owned=True):
    namespace = {"__name__": "preflight_test"}
    exec(preflight.PROBE, namespace)
    service = SimpleNamespace(
        alive=lambda record: child_alive,
        request=lambda port, command: status,
    )
    record = {"ready": True, "child_pid": 42, "child_start_ticks": "123"}
    return namespace["probe_engine"](service, record, 29920, lambda *args: owned)


def test_live_content_is_ready():
    result = engine_probe({"ok": True, "coldReady": True, "contentReady": True})
    assert result["ready"] is True


@pytest.mark.parametrize("status", [None, [], {"ok": True},
                                    {"ok": True, "coldReady": True, "contentReady": False}])
def test_live_supervisor_without_ready_content_is_rejected(status):
    result = engine_probe(status)
    assert result["ready"] is False and result["error"]


@pytest.mark.parametrize("options", [{"child_alive": False}, {"owned": False}])
def test_dead_or_foreign_process_is_rejected(options):
    result = engine_probe({"ok": True, "coldReady": True, "contentReady": True}, **options)
    assert result["ready"] is False and result["error"]


def test_status_failure_is_isolated_to_engine():
    namespace = {"__name__": "preflight_test"}
    exec(preflight.PROBE, namespace)
    def fail_request(port, command):
        raise TimeoutError("status timed out")
    service = SimpleNamespace(alive=lambda record: True, request=fail_request)
    result = namespace["probe_engine"](service, {"ready": True}, 29920, lambda *args: True)
    assert result == {"port": 29920, "ready": False, "error": "status timed out"}
