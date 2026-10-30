"""Exact relocated-branch and marker guards; full-engine parity is independent."""
from pathlib import Path
import re
from types import SimpleNamespace
import pytest
import run_headless_match as runner
from test_run_headless_match import inputs, COMPLETE


def words(name):
    t=Path(__file__).with_name("headless_query_dimension_pair.h").read_text()
    b=t.split("static const uint32_t "+name+"[] = {",1)[1].split("};",1)[0]
    return [int(v,16) for v in re.findall(r"0x[0-9a-f]+",b)]


def branches(values):
    result=[]
    for i,w in enumerate(values):
        pc=0xf34c6c+i*4
        if w&0x7c000000==0x14000000:bits=26;imm=w&0x3ffffff;kind=w&~0x3ffffff
        elif w&0xff000010==0x54000000:bits=19;imm=(w>>5)&0x7ffff;kind=w&~(0x7ffff<<5)
        elif w&0x7e000000==0x34000000:bits=19;imm=(w>>5)&0x7ffff;kind=w&~(0x7ffff<<5)
        elif w&0x7e000000==0x36000000:bits=14;imm=(w>>5)&0x3fff;kind=w&~(0x3fff<<5)
        else:continue
        if imm&(1<<(bits-1)):imm-=1<<bits
        result.append((kind,pc+imm*4))
    return result


def test_every_relocated_branch_preserves_original_target_and_condition():
    old,new=words("query_dimension_original"),words("query_dimension_replacement")
    assert len(old)==len(new)==19
    assert branches(old)==branches(new)
    assert new[0]==0x294026e8 and new[-1]==0xd503201f
    assert old[0]==0xb94002e8 and old[4]==0xb94006e9
    assert old[7:10]==new[6:9]
    assert old[11:13]==new[10:12] and old[14:16]==new[13:15]


@pytest.mark.parametrize("index",range(5))
def test_query_combination_requires_each_child_marker(inputs,monkeypatch,index):
    settings,output=inputs;markers=runner.optimization_installation_markers("hot-distance-query-v1")
    assert len(markers)==5;markers.pop(index)
    def execute(command,*,env,stdout,stderr):
        stdout.write((chr(10).join(markers)+chr(10)+COMPLETE).encode());return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess,"run",execute)
    with pytest.raises(ValueError,match="installation evidence"):
        runner.run(settings,output,apply=True,snapshot_trace=False,native_optimization="hot-distance-query-v1")


def test_no_dimension_cache_and_all_original_bytes_guarded():
    t=Path(__file__).with_name("headless_query_dimension_pair.h").read_text()
    assert "memcmp(target, query_dimension_original, bytes)" in t
    assert "PROT_READ | PROT_WRITE | PROT_EXEC" not in t
    assert "valid complete grid object" in t
