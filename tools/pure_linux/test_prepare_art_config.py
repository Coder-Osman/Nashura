import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from prepare_art_config import prepare


STOCK = b"""dir.art = /apex/com.android.art/bin/
[art]
additional.namespaces = system,conscrypt,art,neuralnetworks,adbd
namespace.default.links = art,system,adbd
namespace.default.link.art.allow_all_shared_libs = true
namespace.art.visible = true
namespace.art.links = system
namespace.system.links = art,adbd
namespace.system.link.art.shared_libs = libnativehelper.so
namespace.system.search.paths = /system/${LIB}
namespace.conscrypt.links = art,system
namespace.conscrypt.visible = true
"""


def test_generated_namespace_contract():
    text = prepare(STOCK, hashlib.sha256(STOCK).hexdigest())
    assert "namespace.com_android_art.visible = true" in text
    assert "namespace.system.link.com_android_art.shared_libs = libnativehelper.so" in text
    assert "namespace.com_android_conscrypt.links = com_android_art,system" in text
    assert "namespace.system.permitted.paths += /apex/com.android.os.statsd/${LIB}" in text
    assert "namespace.art." not in text
    assert "namespace.conscrypt." not in text


def test_identity_and_unexpected_schema_fail_closed():
    with pytest.raises(ValueError, match="identity mismatch"):
        prepare(STOCK + b"# modified\n", hashlib.sha256(STOCK).hexdigest())
    for content in (STOCK.replace(b",adbd", b",adbd,extra"), STOCK + STOCK):
        with pytest.raises(ValueError, match="unexpected stock"):
            prepare(content, hashlib.sha256(content).hexdigest())


def test_cli_does_not_replace_output_or_create_on_identity_failure(tmp_path):
    source, output = tmp_path / "stock.txt", tmp_path / "generated.txt"
    source.write_bytes(STOCK)
    command = [sys.executable, str(Path(__file__).with_name("prepare_art_config.py")),
               "--stock", str(source), "--stock-sha256", "0" * 64, "--output", str(output)]
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert not output.exists()
    command[command.index("--stock-sha256") + 1] = hashlib.sha256(STOCK).hexdigest()
    assert subprocess.run(command, capture_output=True).returncode == 0
    before = output.read_bytes()
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert output.read_bytes() == before
