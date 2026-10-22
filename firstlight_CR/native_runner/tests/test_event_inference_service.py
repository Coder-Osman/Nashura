"""Microbatching preserves action likelihood, masks, and per-match randomness."""
from dataclasses import fields
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import torch

from native_runner.tests.test_event_model import _event_model, _batch
from native_runner.training.event_inference_service import Service, infer_batch, receive, send
from native_runner.training.event_delay_constraints import playable_delay_mask


def test_persistent_connection_survives_idle_longer_than_frame_timeout():
    class ShortDeadlineSocket(socket.socket):
        def settimeout(self, value):
            super().settimeout(None if value is None else .05)

    client, peer = socket.socketpair()
    server = ShortDeadlineSocket(fileno=peer.detach())
    client.settimeout(2.)
    service = Service("cpu")
    thread = threading.Thread(target=service.connection, args=(server,), daemon=True)
    thread.start()
    try:
        for request_id in (1, 2):
            time.sleep(.15)
            send(client, dict(operation="describe", request_id=request_id))
            request, future, _ = service.inbox.get(timeout=2.)
            assert request["request_id"] == request_id
            future.set_result(dict(ok=True, request_id=request_id))
            assert receive(client) == dict(ok=True, request_id=request_id)
        # A partial next frame still expires; it must never enter the inbox.
        client.sendall(b"\x00")
        thread.join(timeout=2.)
        assert not thread.is_alive()
        assert service.inbox.empty()
        assert client.recv(1) == b""
    finally:
        client.close()
        thread.join(timeout=2.)
        server.close()


def test_idle_connection_peer_close_exits_handler():
    client, server = socket.socketpair()
    service = Service("cpu")
    thread = threading.Thread(target=service.connection, args=(server,), daemon=True)
    thread.start()
    client.close()
    thread.join(timeout=2.)
    assert not thread.is_alive()


def test_client_response_keeps_deadline():
    client, server = socket.socketpair()
    try:
        client.settimeout(.05)
        with pytest.raises(socket.timeout):
            receive(client)
    finally:
        client.close()
        server.close()


def requests(count=4):
    return [dict(frame=_batch(1), gate_mask=torch.tensor([[True, i != 0]]),
                 card_mask=torch.tensor([[True, True, False, True, True, False, True, True]]),
                 cell_masks=torch.ones(1, 8, 576, dtype=torch.bool),
                 delay_mask=torch.ones(1, 8, 61, dtype=torch.bool),
                 temperatures=(.9, 1.1, .8, 1.2),
                 uniforms=torch.rand(1, 4, generator=torch.Generator().manual_seed(100+i), dtype=torch.float64))
            for i in range(count)]


@pytest.mark.parametrize("constrained", [False, True])
def test_batch_order_and_tail_preserve_actions_and_replayed_likelihood(constrained):
    model = _event_model().eval()
    reqs = requests()
    if constrained:
        with torch.no_grad():
            model.delay_head[-1].bias[0] = 10000
        for request in reqs:
            request["delay_mask"] &= torch.tensor(playable_delay_mask(model.event_config.delay_offset_ms))
    with torch.inference_mode():
        batched = infer_batch(model, reqs)
        reversed_steps = infer_batch(model, reqs[::-1])[::-1]
        for request, actual, reversed_step in zip(reqs, batched, reversed_steps):
            single = infer_batch(model, [request])[0]
            if constrained and actual.action.act.item():
                assert actual.action.delay_bin.item() >= 1
            for name in ("act", "card_row", "cell", "delay_bin"):
                assert torch.equal(getattr(actual.action, name), getattr(single.action, name))
                assert torch.equal(getattr(actual.action, name), getattr(reversed_step.action, name))
            context = model.encode(request["frame"], model.initial_state(1), episode_start=torch.ones(1, dtype=torch.bool))
            replay = model.evaluate_actions(context, actual.action, **{
                k: request[k] for k in ("gate_mask", "card_mask", "cell_masks", "delay_mask", "temperatures")})
            for f in fields(actual):
                if f.name not in ("action", "temperatures"):
                    torch.testing.assert_close(getattr(actual, f.name), getattr(replay, f.name), atol=2e-5, rtol=2e-5)
    assert not batched[0].action.act.item()


def test_service_rejects_stale_identity():
    service = Service("cpu")
    service.identity = ("new", "source", "contract")
    with pytest.raises(ValueError, match="stale"):
        service.control(dict(operation="describe", identity=("old", "source", "contract")))


def test_wire_roundtrip_preserves_cpu_tensor_record():
    first, second = socket.socketpair()
    request = requests(1)[0]
    thread = threading.Thread(target=send, args=(first, request))
    thread.start()
    restored = receive(second)
    thread.join(timeout=5)
    first.close()
    second.close()
    assert not thread.is_alive()
    torch.testing.assert_close(restored["uniforms"], request["uniforms"], atol=0, rtol=0)
    torch.testing.assert_close(restored["frame"].match_scalars, request["frame"].match_scalars, atol=0, rtol=0)


def test_uniform_rejects_invalid_values():
    model = _event_model().eval()
    request = requests(1)[0]
    request["uniforms"][0, 0] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        infer_batch(model, [request])


def test_socket_concurrency_tail_and_weight_barrier(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from native_runner.training.event_inference_service import Client, identity

    model = _event_model().eval()
    descriptor = dict(artifact_sha256="a", source_checkpoint_sha256="b", contract_sha256="c")
    service = Service("cpu", max_batch=4, wait_ms=20)
    service.weights = SimpleNamespace(model=model, load=lambda d: (model, "test-policy"))
    import tempfile
    socket_directory = tempfile.TemporaryDirectory(prefix="cr-test-", dir="/tmp")
    path = Path(socket_directory.name) / "s"
    thread = threading.Thread(target=service.run, args=(path,), daemon=True)
    thread.start()
    for _ in range(200):
        if path.exists():
            break
        time.sleep(.005)
    controller = Client(path)
    clients = []
    try:
        controller.request("load", descriptor=descriptor)
        for seed in (71, 72, 73):
            client = Client(path)
            _, policy = client.start_match(descriptor, seed)
            assert policy == "test-policy"
            clients.append(client)
        barrier = threading.Barrier(3)
        reqs = requests(3)
        def run(i):
            request = reqs[i]
            barrier.wait(timeout=10)
            return clients[i].infer(request["frame"], owner=0, tick=5,
                                    **{k:v for k,v in request.items() if k not in ("frame", "uniforms")})
        with ThreadPoolExecutor(3) as pool:
            steps = list(pool.map(run, range(3)))
        for i, (req, step) in enumerate(zip(reqs, steps)):
            req["uniforms"] = torch.rand((1,4), generator=torch.Generator().manual_seed(71+i), dtype=torch.float64)
            expected = infer_batch(model, [req])[0]
            torch.testing.assert_close(step.log_prob, expected.log_prob, atol=2e-5, rtol=2e-5)
            assert torch.equal(step.action.act, expected.action.act)
        # A lone tail request completes; it never waits for the maximum batch.
        req = reqs[0]
        clients[0].infer(req["frame"], owner=1, tick=6,
                         **{k:v for k,v in req.items() if k not in ("frame", "uniforms")})
        replacement = {**descriptor, "artifact_sha256":"next", "source_checkpoint_sha256":"next-source"}
        controller.request("load", descriptor=replacement)
        with pytest.raises(RuntimeError, match="stale"):
            clients[0].infer(req["frame"], owner=0, tick=7,
                             **{k:v for k,v in req.items() if k not in ("frame", "uniforms")})
        clients[0].start_match(replacement, 71)
        step = clients[0].infer(req["frame"], owner=0, tick=8,
                               **{k:v for k,v in req.items() if k not in ("frame", "uniforms")})
        torch.testing.assert_close(step.log_prob, infer_batch(model, [req])[0].log_prob, atol=2e-5, rtol=2e-5)
    finally:
        for client in clients:
            client.close()
        controller.request("shutdown")
        controller.close()
        thread.join(timeout=10)
        socket_directory.cleanup()
    assert not thread.is_alive()


def test_failed_weight_load_invalidates_old_behavior_identity():
    service = Service("cpu")
    descriptor = dict(artifact_sha256="old", source_checkpoint_sha256="source", contract_sha256="contract")
    service.identity = tuple(descriptor.values())
    def fail(_):
        raise ValueError("partial load rejected")
    service.weights = SimpleNamespace(load=fail)
    with pytest.raises(ValueError, match="partial"):
        service.control(dict(operation="load", descriptor=descriptor))
    with pytest.raises(ValueError, match="stale"):
        service.control(dict(operation="describe", identity=tuple(descriptor.values())))


def test_uniform_endpoints_never_select_masked_trailing_entries():
    model = _event_model().eval()
    req = requests(1)[0]
    req["gate_mask"][:] = True
    req["gate_mask"][0, 0] = False
    for name in ("card_mask", "cell_masks", "delay_mask"):
        req[name][:] = False
        req[name][..., 0] = True
    for value in (0., torch.nextafter(torch.tensor(1., dtype=torch.float64), torch.tensor(0., dtype=torch.float64)).item()):
        req["uniforms"][:] = value
        step = infer_batch(model, [req])[0]
        assert step.action.act.item()
        assert step.action.card_row.item() == step.action.cell.item() == step.action.delay_bin.item() == 0


def test_tensor_wire_unpickles_from_a_separate_module_entrypoint():
    import pickle
    import subprocess
    import sys
    # CLI module globals have a different module name than imported worker
    # globals. No reducer may refer to that transient entrypoint namespace.
    script = """
import io,runpy,sys,torch
namespace=runpy.run_module('native_runner.training.event_inference_service',run_name='wire_worker')
buffer=io.BytesIO()
namespace['TensorPickler'](buffer,protocol=5).dump({'x':torch.arange(8).reshape(2,4)})
sys.stdout.buffer.write(buffer.getvalue())
"""
    completed = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)
    restored = pickle.loads(completed.stdout)
    torch.testing.assert_close(restored["x"], torch.arange(8).reshape(2,4), rtol=0, atol=0)
