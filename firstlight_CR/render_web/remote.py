import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import threading
import time

import paramiko

FILES = (
    "remote_agent.py",
    "catalog_metadata.py",
    "generate_match.py",
    "setup_remote_avd.py",
    "runtime_constraints.py",
    "event_delay_constraints.py",
    "checkpoint_sources.py",
)
ARTIFACTS = (
    "stats.json",
    "initial.json",
    "render-match.json",
    "native-actions.json",
    "trajectory.jsonl",
    "runtime-manifest.json",
    "engine-attestation.json",
    "shutdown.json",
    "generation.log",
)


class Remote:
    def __init__(self, config):
        self.config = config
        self.package = None
        self.deploy_lock = threading.Lock()

    def connect(self):
        c = self.config
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            c.ssh_host,
            username=c.ssh_user,
            password=c.ssh_password or None,
            key_filename=c.ssh_key or None,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )
        return client

    @staticmethod
    def mkdir(sftp, path):
        path = PurePosixPath(path)
        try:
            sftp.stat(str(path))
        except FileNotFoundError:
            Remote.mkdir(sftp, path.parent)
            sftp.mkdir(str(path))

    def deploy(self):
        with self.deploy_lock:
            if self.package:
                return
            source = Path(__file__).parent
            sources = {name: source / name for name in FILES}
            sources["event_delay_constraints.py"] = source.parent / "native_runner/training/event_delay_constraints.py"
            digest = hashlib.sha256(
                b"".join(sources[n].read_bytes() for n in FILES)
            ).hexdigest()[:20]
            package = self.config.remote_root + "/packages/" + digest
            with self.connect() as client, client.open_sftp() as sftp:
                self.mkdir(sftp, package)
                for name in FILES:
                    target = package + "/" + name
                    try:
                        with sftp.open(target) as file:
                            existing = file.read()
                        if existing != sources[name].read_bytes():
                            raise RuntimeError("远端包内容不一致")
                    except FileNotFoundError:
                        sftp.put(str(sources[name]), target)
            self.package = package

    def call(self, command, identity=None, timeout=300):
        self.deploy()
        args = [
            "python3",
            self.package + "/remote_agent.py",
            command,
            "--config",
            json.dumps(self.config.remote()),
        ]
        if identity:
            args += ["--id", identity]
        with self.connect() as client:
            _, stdout, stderr = client.exec_command(shlex.join(args), timeout=timeout)
            result = stdout.read().decode()
            error = stderr.read().decode()
            if stdout.channel.recv_exit_status():
                raise RuntimeError(error[-2000:] or "远端请求失败")
        return json.loads(result)

    def submit(self, job):
        self.deploy()
        remote = self.config.remote_root + "/jobs/" + job["id"]
        request = job["request"]
        with self.connect() as client, client.open_sftp() as sftp:
            self.mkdir(sftp, remote)
            content = json.dumps(request, sort_keys=True)
            try:
                with sftp.open(remote + "/request.json") as file:
                    existing = json.loads(file.read())
                if existing != request:
                    raise ValueError("远端任务参数身份冲突")
            except FileNotFoundError:
                with sftp.open(remote + "/request.json.tmp", "w") as file:
                    file.write(content)
                sftp.rename(remote + "/request.json.tmp", remote + "/request.json")
        return self.call("start", job["id"])

    def fetch(self, identity, directory):
        directory = Path(directory)
        remote = self.config.remote_root + "/jobs/" + identity
        with self.connect() as client, client.open_sftp() as sftp:
            for name in ARTIFACTS:
                temp = directory / (name + ".download")
                sftp.get(remote + "/" + name, str(temp))
                temp.replace(directory / name)
