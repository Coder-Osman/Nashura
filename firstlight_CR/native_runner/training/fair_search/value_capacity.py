import json
from pathlib import Path


HOSTS = ("34", "35", "37", "39", "41")


def capacity_config(root):
    config = dict(stages=[1, 2, 4], observation_seconds=600,
                  limits={host: 1 if host == "34" else 4 for host in HOSTS},
                  devices={host: [6] if host == "34" else list(range(4)) for host in HOSTS},
                  drain_hosts=[])
    path = Path(root) / "capacity.json"
    if path.exists():
        config.update(json.loads(path.read_text()))
    stages = config["stages"]
    if (not stages or stages[0] != 1 or any(type(stage) is not int or stage < 1 for stage in stages)
            or stages != sorted(set(stages)) or config["observation_seconds"] < 600):
        raise ValueError("invalid capacity stages")
    if set(config["limits"]) != set(HOSTS) or set(config["devices"]) != set(HOSTS):
        raise ValueError("capacity hosts differ")
    for host in HOSTS:
        devices = config["devices"][host]
        limit = config["limits"][host]
        if (type(limit) is not int or not 1 <= limit <= 32 or not devices
                or any(type(device) is not int or device < 0 for device in devices)
                or len(set(devices)) != len(devices)):
            raise ValueError("invalid actor limit or GPU mapping")
    if config["limits"]["34"] != 1 or not set(config["drain_hosts"]) <= set(HOSTS):
        raise ValueError("invalid learner reserve or drain hosts")
    return config


def actor_limit(config, host, stage):
    return 0 if host in config["drain_hosts"] else min(config["limits"][host], stage)


def actor_device(config, host, slot):
    if type(slot) is not int or not 0 <= slot < config["limits"][host]:
        raise ValueError("actor slot outside capacity")
    devices = config["devices"][host]
    return devices[slot % len(devices)]
