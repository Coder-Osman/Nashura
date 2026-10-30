"""Exact instruction and launch guards; real ARM64 parity is independent."""
from pathlib import Path
import re
from types import SimpleNamespace
import pytest
import run_headless_match as runner
from test_run_headless_match import inputs, COMPLETE

def words(name):
    text=Path(__file__).with_name("headless_grid_clear_unroll.h").read_text()
    body=text.split("static const uint32_t "+name+"[] = {",1)[1].split("};",1)[0]
    return [int(x,16) for x in re.findall(r"0x[0-9a-f]+",body)]

def test_exact_extent_and_pairwise_access_order():
    old,new=words("grid_clear_original"),words("grid_clear_replacement")
    assert len(old)==len(new)==28
    assert old[:4]==new[:4]
    assert new[8:14]==[0xa8c12d2a,0xb9000d5f,0xb9000d7f]*2
    assert new[19:22]==new[8:11]
    assert new[-4:]==[0xf940012a,0xb9000d5f,0xd65f03c0,0xd503201f]

@pytest.mark.parametrize("index",range(5))
def test_combined_marker_missing_rejected(inputs,monkeypatch,index):
    settings,output=inputs
    markers=runner.optimization_installation_markers("hot-distance-grid-v1")
    assert len(markers)==5
    markers.pop(index)
    def execute(command,*,env,stdout,stderr):
        stdout.write((chr(10).join(markers)+chr(10)+COMPLETE).encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess,"run",execute)
    with pytest.raises(ValueError,match="installation evidence"):
        runner.run(settings,output,apply=True,snapshot_trace=False,native_optimization="hot-distance-grid-v1")

def test_default_unchanged_and_no_wx_page():
    text=Path(__file__).with_name("headless_grid_clear_unroll.h").read_text()
    assert "PROT_READ | PROT_WRITE | PROT_EXEC" not in text
    assert "memcmp(target, grid_clear_original, bytes)" in text
    assert "__builtin___clear_cache" in text
