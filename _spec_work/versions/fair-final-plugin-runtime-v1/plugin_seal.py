import json
from pathlib import Path

from plugin_protocol import sha, validate_seal
from verify_trace import validate_trajectory


def seal_identity(folder, report, cleanup):
    process = cleanup.get("process", {})
    if (cleanup.get("clean") is not True or cleanup.get("port_released") is not True
            or cleanup.get("exit_code") != 0 or process.get("phase") != "stopped"
            or type(process.get("child_pid")) is not int or process["child_pid"] <= 0
            or not isinstance(process.get("child_start_ticks"), str)):
        raise ValueError("plugin supervised seal requires owned clean shutdown")
    validate_trajectory(report)
    if (report.get("status") != "completed_pending_trace_verification"
            or report.get("mode") != "trace" or report.get("training_admissible") is not False
            or report.get("gates_closed") is not False):
        raise ValueError("plugin supervised seal requires complete native trajectory")
    path = folder / "qemu.log"
    value = dict(schema="FairFinalSupervisedTraceSeal/v1",
        closure="owned_process_stopped_after_native_terminal", trace_sha256=sha(path),
        trace_bytes=path.stat().st_size, final_offset=report["final_offset"],
        report_sha256=sha(folder / "report.json"), cleanup_sha256=sha(folder / "cleanup.json"),
        child_pid=process["child_pid"], child_start_ticks=process["child_start_ticks"],
        training_admissible=False, gates_closed=False)
    validate_seal(path, value)
    return value


def create_seal(folder):
    from selected_collection_worker import matching_process, write
    report = json.loads((folder / "report.json").read_text())
    cleanup = json.loads((folder / "cleanup.json").read_text())
    if json.loads((folder / "probe-process.json").read_text()).get("exit_code") != 0:
        raise ValueError("plugin supervised seal requires successful native probe")
    value = seal_identity(folder, report, cleanup)
    if matching_process(value["child_pid"], value["child_start_ticks"]):
        raise ValueError("plugin process still alive at supervised seal")
    write(folder / "trace-seal.json", value)
    return value
