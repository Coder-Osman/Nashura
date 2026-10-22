import hashlib
from types import SimpleNamespace

import fleet_deploy as deploy


def test_task_config_is_part_of_deployment_verification():
    assert deploy.TASK_CONFIG_PATH in deploy.FILES


def test_fixed_config_is_installed_atomically(tmp_path, monkeypatch):
    source = tmp_path / "config.json"
    source.write_text('{"fleet": {"34": 24}}\n')
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    operations = []
    class Sftp:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def put(self, source_name, target_name):
            operations.append(("put", source_name, target_name))
        def posix_rename(self, source_name, target_name):
            operations.append(("rename", source_name, target_name))
    client = SimpleNamespace(open_sftp=Sftp)
    target = "/remote/project/" + deploy.TASK_CONFIG_PATH
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    assert deploy.install_task_config(client, "/remote/project") == expected
    assert operations == [("put", str(source), target + ".deploying"),
                          ("rename", target + ".deploying", target)]
