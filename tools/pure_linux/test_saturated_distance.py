"""Integer reference boundaries; actual instruction equivalence uses ARM64 harness."""
import itertools
import random
from pathlib import Path
import re


def original(dx,dy):
    mask=(1<<32)-1
    if ((dx-46341)&mask)<0xfffe95f7 or ((dy-46341)&mask)<0xfffe95f7:
        return 0x7fffffff
    x=(dx*dx)&mask;y=(dy*dy)&mask
    return (x+y)&mask if y<(x^0x7fffffff) else 0x7fffffff


def widened(dx,dy):
    signed=lambda x:(x&0x7fffffff)-(x&0x80000000)
    return min(signed(dx)**2+signed(dy)**2,0x7fffffff)


def test_modular_differences_and_saturation_boundaries():
    edges=[0,1,32767,32768,46339,46340,46341,46342,0x7fffffff,0x80000000,0xffffffff]
    edges.extend((-x)&0xffffffff for x in list(edges))
    for dx,dy in itertools.product(edges,repeat=2):
        assert original(dx,dy)==widened(dx,dy)
    rng=random.Random(20261031)
    for _ in range(20000):
        dx,dy=(rng.getrandbits(32) for _ in range(2))
        assert original(dx,dy)==widened(dx,dy)


def words(name):
    text=Path(__file__).with_name("headless_saturated_distance.h").read_text()
    body=text.split("static const uint32_t "+name+"[] = {",1)[1].split("};",1)[0]
    return [int(x,16) for x in re.findall(r"0x[0-9a-f]+",body)]


def test_conditional_pointer_load_and_original_function_extents():
    pointer=words("distance_pointer_replacement")
    scalar=words("distance_scalar_replacement")
    assert len(words("distance_pointer_original"))*4==0x121b8bc-0x121b860
    assert len(words("distance_scalar_original"))*4==0x121b910-0x121b8bc
    assert len(pointer)==15 and len(scalar)==9
    branch=pointer[5];assert branch&0xff00001f==0x54000002
    assert 5*4+((branch>>5)&0x7ffff)*4==13*4
    assert pointer[6]==0xb9400409 and pointer[13:]==[0x2a0a03e0,0xd65f03c0]
    assert scalar[2:4]==[0x9b287d08,0x9b297d29]


def test_installer_verifies_both_full_originals_and_keeps_wx_separate():
    text=Path(__file__).with_name("headless_saturated_distance.h").read_text()
    assert "original[i],original_sizes[i]" in text
    assert "PROT_READ | PROT_WRITE | PROT_EXEC" not in text
    assert "__builtin___clear_cache" in text and "0xd503201f" in text
    assert "if (installed) return 1;" in text
