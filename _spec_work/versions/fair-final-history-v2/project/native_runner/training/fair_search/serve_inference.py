import argparse
import hashlib
import json
from pathlib import Path
import signal
import threading

import torch

from .config import SearchConfig, load_search_config
from .inference import BatchInference, LocalInference, SocketInferenceServer
from .model import load_checkpoint


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve frozen FAIR checkpoints to local actors.")
    parser.add_argument("--checkpoint", type=Path, action="append", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--batch", type=int)
    parser.add_argument("--wait-ms", type=float)
    parser.add_argument("--queue", type=int)
    parser.add_argument("--search-config", type=Path)
    args = parser.parse_args(argv)
    torch.set_num_threads(1)
    config = (load_search_config(args.search_config) if args.search_config else SearchConfig(
        inference_batch=32 if args.batch is None else args.batch,
        inference_wait_ms=2. if args.wait_ms is None else args.wait_ms,
        inference_queue=128 if args.queue is None else args.queue))
    for override, expected in ((args.batch, config.inference_batch), (args.wait_ms, config.inference_wait_ms),
                               (args.queue, config.inference_queue)):
        if override is not None and override != expected:
            raise ValueError("inference override differs from explicit search configuration")
    backends = {}
    for checkpoint in args.checkpoint:
        model, payload, identity = load_checkpoint(checkpoint, device=args.device)
        if args.search_config and payload["config_sha256"] != config.sha256:
            raise ValueError("inference checkpoint search configuration differs")
        if identity in backends:
            raise ValueError("duplicate checkpoint identity")
        backends[identity] = LocalInference(model, identity, config=config)
    stopped = threading.Event()
    previous = {name: signal.signal(name, lambda *_: stopped.set()) for name in (signal.SIGTERM, signal.SIGINT)}
    batch = BatchInference(backends, config=config)
    server = None
    try:
        server = SocketInferenceServer(args.directory, batch)
        print(json.dumps(dict(stage="ready", socket=str(server.path), identities=sorted(backends),
                              inference_sha256=hashlib.sha256(Path(__file__).with_name("inference.py").read_bytes()).hexdigest(),
                              config_sha256=config.sha256, batch=config.inference_batch,
                              wait_ms=config.inference_wait_ms, queue=config.inference_queue)), flush=True)
        stopped.wait()
    finally:
        server.close() if server is not None else batch.close()
        for name, handler in previous.items():
            signal.signal(name, handler)
        print(json.dumps(dict(stage="stopped", batch_profile=batch.profile,
                              models={identity: backend.profile for identity, backend in backends.items()})), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
