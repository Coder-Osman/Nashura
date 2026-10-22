"""CUDA-event timings without synchronizing each row batch."""
from collections import defaultdict
import time

import torch


class DeviceTimings:
    def __init__(self, device):
        self.device = device
        self.pending = {}
        self.events = defaultdict(list)
        self.wall = defaultdict(float)

    def start(self, name):
        event = None
        if self.device.type == "cuda":
            event = torch.cuda.Event(enable_timing=True)
            event.record(torch.cuda.current_stream(self.device))
        self.pending[name] = (time.perf_counter(), event)

    def stop(self, name):
        started, event = self.pending.pop(name)
        self.wall[name] += time.perf_counter() - started
        if event is not None:
            end = torch.cuda.Event(enable_timing=True)
            end.record(torch.cuda.current_stream(self.device))
            self.events[name].append((event, end))

    def result(self):
        result = {name+"_host_seconds": seconds for name, seconds in self.wall.items()}
        if self.events:
            torch.cuda.synchronize(self.device)
            result.update({name+"_gpu_seconds": sum(a.elapsed_time(b) for a,b in pairs)/1000
                           for name,pairs in self.events.items()})
        return result
