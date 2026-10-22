"""Compile and exercise the actual C++ capture index against the old scan."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_native_capture_index_matches_linear_validation(tmp_path):
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    include = Path(__file__).resolve().parents[1] / "probe/capture_scene_index.inc"
    source = tmp_path / "index_test.cpp"
    source.write_text(r"""
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <random>
constexpr int kMaxObservedObjects = 256;
bool process_range_is_readable(const void *p, int) { return p != reinterpret_cast<void *>(1); }
template<class T> T read_object_field(const void *p, int offset) {
 T value; std::memcpy(&value, static_cast<const char *>(p) + offset, sizeof(value)); return value;
}
#include """ + '"' + str(include) + '"\n' + r"""
struct Object { std::uint64_t padding; std::uint32_t id; };
bool old_unique(const void *pointer, void *const *objects, int count) {
 if (!pointer) return false;
 int pc=0, ic=0;
 const auto id=read_object_field<std::uint32_t>(pointer, 8);
 if (!id) return false;
 for(int i=0;i<count;++i) {
   if(!objects[i]) continue;
   if(!process_range_is_readable(objects[i],12)) return false;
   const auto cid=read_object_field<std::uint32_t>(objects[i],8);
   if(!cid) return false;
   pc += objects[i] == pointer; ic += cid == id;
 }
 return pc == 1 && ic == 1;
}
int main() {
 std::mt19937 rng(719);
 Object values[64]; void *objects[80];
 for(int trial=0;trial<2000;++trial) {
   for(int i=0;i<64;++i) values[i].id = trial%3 == 0 ? rng()%50 : i+1;
   int count = rng()%81;
   for(int i=0;i<count;++i) { int slot=rng()%66; objects[i]=slot<64 ? &values[slot] : nullptr; }
   if(trial%17==0 && count) objects[count-1]=reinterpret_cast<void *>(1);
   {
     CaptureSceneIndexScope scope;
     auto *index=capture_scene_index(objects,count);
     for(int i=0;i<64;++i) assert((index->find(&values[i]) != nullptr) == old_unique(&values[i],objects,count));
     { CaptureSceneIndexScope nested; assert(g_capture_scene_index == &nested.index); }
     assert(g_capture_scene_index == &scope.index);
   }
   assert(g_capture_scene_index == nullptr);
 }
 // Same vector and addresses at a new tick must rebuild after identity reuse.
 objects[0]=&values[0]; values[0].id=1;
 { CaptureSceneIndexScope s; assert(capture_scene_index(objects,1)->find(objects[0])->id == 1); }
 values[0].id=77;
 { CaptureSceneIndexScope s; assert(capture_scene_index(objects,1)->find(objects[0])->id == 77); }
}
""")
    executable = tmp_path / "index_test"
    subprocess.run([compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", str(source), "-o", str(executable)], check=True, capture_output=True)
    subprocess.run([str(executable)], check=True, capture_output=True)
