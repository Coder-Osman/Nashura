from pathlib import Path
import socket
import struct
import threading

import pytest

from probe_init_property_service import (
    PROPERTY_NAME, PROPERTY_VALUE, PROP_MSG_SETPROP2, PROP_ERROR_PERMISSION_DENIED,
    property_request, summarize, validate_paths,
)


@pytest.mark.parametrize("response,status", [
    (PROP_ERROR_PERMISSION_DENIED, "INIT_PROPERTY_REQUEST_REJECTED_LIMITED"),
    (0, "INIT_PROPERTY_WRITE_ACK_ONLY_LIMITED"),
    (None, "INIT_PROPERTY_SOCKET_ONLY_LIMITED"),
    (0x10, "INIT_PROPERTY_SOCKET_ONLY_LIMITED"),
])
def test_transport_outcome_never_opens_android_or_game_gates(response, status):
    result = summarize({"connected": True, "response_code": response}, True, 6)
    assert result["status"] == status
    for key in ("property_write_verified", "system_services_ready",
                "game_context_initialized", "catalog_ready_pass"):
        assert result[key] is False


def test_no_response_or_connected_socket_is_not_write_evidence():
    result = summarize({"connected": False, "response_code": 0}, False, 0)
    assert result["status"] == "INIT_PROPERTY_BLOCKED_LIMITED"


def test_matching_readback_is_required_even_when_flag_is_set():
    request = {"connected": True, "response_code": 0, "value": "3", "readback_value": "3",
               "readback_exit": 0, "readback_verified": True}
    result = summarize(request, True, -9)
    assert result["status"] == "INIT_PROPERTY_WRITE_READBACK_PASS_LIMITED"
    assert result["property_write_verified"] is True
    assert result["system_services_ready"] is False
    assert result["catalog_ready_pass"] is False
    for field, damaged in (("response_code", 24), ("readback_value", "2"), ("readback_exit", 1),
                           ("connected", False), ("readback_verified", False)):
        assert summarize(dict(request, **{field: damaged}), True, 0)["property_write_verified"] is False


@pytest.mark.parametrize("truncated", [False, True])
def test_protocol_encoding_and_truncated_response(tmp_path, monkeypatch, truncated):
    # macOS pytest roots can already exceed sockaddr_un.sun_path.
    monkeypatch.chdir(tmp_path)
    path = Path("service")
    expected_name, expected_value = PROPERTY_NAME.encode(), PROPERTY_VALUE.encode()
    expected = (struct.pack("<II", PROP_MSG_SETPROP2, len(expected_name)) + expected_name
                + struct.pack("<I", len(expected_value)) + expected_value)
    observed = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)

        def respond():
            with server.accept()[0] as connection:
                data = bytearray()
                while len(data) < len(expected):
                    data.extend(connection.recv(len(expected) - len(data)))
                observed.append(bytes(data))
                data = struct.pack("<I", PROP_ERROR_PERMISSION_DENIED)
                connection.sendall(data[:2])
                if not truncated:
                    connection.sendall(data[2:])

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            result = property_request(path)
        finally:
            worker.join(timeout=3)
        assert not worker.is_alive()
    assert observed == [expected]
    assert result["connected"] is True
    assert result["readback_verified"] is False
    if truncated:
        assert result["response_code"] is None
        assert result["error"] == "EOFError"
    else:
        assert result["response_code"] == PROP_ERROR_PERMISSION_DENIED


def test_new_output_is_required_and_must_not_be_inside_inputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    closure, vendor = Path("closure"), Path("vendor")
    closure.mkdir()
    vendor.mkdir()
    launcher = Path("launcher.sh")
    launcher.write_text("#!/bin/sh\n")
    validate_paths(closure, vendor, launcher, Path("new-output"))
    for output in (closure, vendor, closure / "output", vendor / "output", Path("/")):
        with pytest.raises(ValueError):
            validate_paths(closure, vendor, launcher, output)
    assert not (closure / "output").exists()
    assert not (vendor / "output").exists()


def test_long_unix_socket_address_is_rejected_before_output_creation(tmp_path):
    launcher = tmp_path / "launcher.sh"
    launcher.touch()
    output = tmp_path / ("x" * 104)
    with pytest.raises(ValueError, match="socket address"):
        validate_paths(tmp_path, tmp_path, launcher, output)
    assert not output.exists()
