"""Composition boundaries and marker evidence; native parity is a separate gate."""
from pathlib import Path
import re
from types import SimpleNamespace
import pytest
import run_headless_match as runner
from test_run_headless_match import inputs, COMPLETE


def test_write_spans_are_disjoint_and_original_getters_only():
    source=Path(__file__).with_name("headless_hot_getters.h").read_text()
    body=source.split("} hot_getter_sites[] = {",1)[1].split("};",1)[0]
    sites=[int(x,16) for x in re.findall(r"[{](0x[0-9a-f]+),",body)]
    assert len(sites)==11
    assert all(not 0x121b860 <= pc < 0x121b910 for pc in sites)
    header=Path(__file__).with_name("headless_combined_hot_distance.h").read_text()
    assert "!install_hot_getters(engine) || !install_saturated_distance(engine)" in header
    assert "install_component_getters" not in header
    assert "install_spatial_y_bound" not in header


@pytest.mark.parametrize("mutate", ["missing_hot","missing_distance","missing_combined","duplicate","reverse"])
def test_all_ordered_child_and_combined_installations_required(inputs,monkeypatch,mutate):
    settings,output=inputs
    markers=runner.optimization_installation_markers("hot-distance-v1")
    if mutate.startswith("missing"):
        markers.pop({"missing_hot":0,"missing_distance":1,"missing_combined":2}[mutate])
    elif mutate=="duplicate":markers.append(markers[-1])
    else:markers.reverse()
    def execute(command,*,env,stdout,stderr):
        stdout.write((chr(10).join(markers)+chr(10)+COMPLETE).encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess,"run",execute)
    with pytest.raises(ValueError,match="installation evidence"):
        runner.run(settings,output,apply=True,snapshot_trace=False,native_optimization="hot-distance-v1")
    assert not (output/"result.json").exists()
