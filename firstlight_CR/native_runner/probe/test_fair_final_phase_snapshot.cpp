#include <atomic>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <memory>
#include "phase_runtime_layout.h"
#include "fair_final_phase_observation.h"
#include <thread>

constexpr std::size_t kPhaseRuntimeCacheCapacity = 512;
constexpr std::int32_t kPhaseRuntimeUnset = (-2147483647 - 1);
constexpr std::int32_t kMaxObservedObjects = 256;
struct CombatCaptureContext {
  void *manager;
  void *object_manager;
  std::uint64_t generation;
  std::uint64_t state_epoch;
  std::int32_t tick;
};
struct NativeEntityIdentityView {
  std::uint32_t native_object_id = 0;
  std::int32_t owner = 0;
  std::int32_t object_index = 0;
  std::int32_t secondary_index = 0;
};
struct TestObject { std::uint8_t bytes[0x170] = {}; };
struct TestManager { void *objects[256] = {}; std::int32_t count = 0; };
template<class Value> Value read_object_field(const void *object, std::size_t offset) {
  Value value{};
  std::memcpy(&value, static_cast<const std::uint8_t *>(object) + offset, sizeof value);
  return value;
}
bool process_range_is_readable(const void *pointer, std::size_t) { return pointer != nullptr; }
bool read_native_entity_identity(const void *object, NativeEntityIdentityView *identity) {
  identity->native_object_id = read_object_field<std::uint32_t>(object, 8);
  identity->owner = read_object_field<std::int32_t>(object, 0x78);
  identity->object_index = read_object_field<std::int32_t>(object, 0x94);
  identity->secondary_index = read_object_field<std::int32_t>(object, 0x98);
  return identity->native_object_id != 0;
}
void *combat_object_manager_from_game_manager(void *manager) { return manager; }
bool read_combat_object_vector(void *manager, void ***objects, std::int32_t *count) {
  if (!manager) return false;
  auto *test = static_cast<TestManager *>(manager);
  *objects = test->objects;
  *count = test->count;
  return true;
}
bool phase_component_at(const void *object, std::int32_t, void **component) {
  *component = const_cast<void *>(object);
  return read_object_field<std::int32_t>(object, 0x10) == 1;
}
FAIR_PHASE_PAYLOAD
PhaseRuntimeCache g_phase_runtime_cache[kPhaseRuntimeCacheCapacity];
std::atomic<std::uint64_t> g_phase_runtime_generation{7};
std::atomic<std::uint64_t> g_phase_runtime_state_epoch{11};
std::atomic<std::uint64_t> g_phase_runtime_rejected_count{0};
FAIR_PHASE_RESET
FAIR_PHASE_CACHE
#include "fair_final_phase_snapshot.inc"

void write_field(TestObject &object, std::size_t offset, std::int32_t value) {
  std::memcpy(object.bytes + offset, &value, sizeof value);
}
void reset_epoch(std::uint64_t epoch) {
  for (auto &entry : g_phase_runtime_cache) reset_phase_runtime_cache_payload(&entry);
  g_phase_runtime_state_epoch = epoch;
}
int main() {
  assert(g_fair_final_phase_observation_depth == 0);
  {
    FairFinalPhaseObservationScope outer;
    assert(g_fair_final_phase_observation_depth == 1);
    std::thread reader([] { assert(g_fair_final_phase_observation_depth == 0); });
    reader.join();
    try {
      FairFinalPhaseObservationScope inner;
      assert(g_fair_final_phase_observation_depth == 2);
      throw 1;
    } catch (int) {}
    assert(g_fair_final_phase_observation_depth == 1);
  }
  assert(g_fair_final_phase_observation_depth == 0);
  TestObject original, restored;
  write_field(original, 8, 5000021);
  write_field(original, 0x10, 1);
  write_field(original, 0x15c, 950);
  write_field(original, 0x160, 1000);
  TestManager manager;
  manager.count = 1;
  manager.objects[0] = &original;
  const CombatCaptureContext source{&manager, &manager, 7, 11, 1200};
  assert(update_phase_cache(source, &original, [](auto &cache) {
    cache.last_deploy_scale_input = 50;
    cache.last_deploy_scale_output = 50;
    cache.last_deploy_scale_tick = 1200;
  }));
  auto saved = capture_fair_final_phase_snapshot(&manager, 7, 11, 1200);
  assert(saved && saved->count == 1 && saved->objects[0].cache.object == 0);
  restored = original;
  manager.objects[0] = &restored;
  write_field(restored, 0x160, 300);
  reset_epoch(12);
  assert(restore_fair_final_phase_snapshot(&manager, 7, 12, 1200, saved));
  assert(read_object_field<std::int32_t>(&restored, 0x160) == 1000);
  PhaseRuntimeCache copied;
  assert(copy_phase_cache(7, 12, &restored, &copied) && copied.last_deploy_scale_tick == 1200);
  assert(!copy_phase_cache(7, 11, &restored, &copied));
  write_field(restored, 0x160, 300);
  reset_epoch(13);
  assert(restore_fair_final_phase_snapshot(&manager, 7, 13, 1200, saved));
  assert(read_object_field<std::int32_t>(&restored, 0x160) == 1000);
  assert(!restore_fair_final_phase_snapshot(&manager, 8, 13, 1200, saved));
  assert(!restore_fair_final_phase_snapshot(&manager, 7, 14, 1200, saved));
  assert(!restore_fair_final_phase_snapshot(&manager, 7, 13, 1201, saved));
  write_field(restored, 0x15c, 900);
  write_field(restored, 0x160, 300);
  assert(!restore_fair_final_phase_snapshot(&manager, 7, 13, 1200, saved));
  assert(read_object_field<std::int32_t>(&restored, 0x160) == 300);
  write_field(restored, 0x15c, 950);
  write_field(restored, 8, 5000022);
  assert(!restore_fair_final_phase_snapshot(&manager, 7, 13, 1200, saved));
  assert(!copy_phase_cache(7, 13, &restored, &copied));
  write_field(restored, 8, 5000021);
  manager.objects[1] = &original;
  manager.count = 2;
  assert(!restore_fair_final_phase_snapshot(&manager, 7, 13, 1200, saved));
  assert(!capture_fair_final_phase_snapshot(&manager, 7, 13, 1200));
  manager.count = 0;
  assert(!restore_fair_final_phase_snapshot(&manager, 7, 13, 1200, saved));
  manager.count = 257;
  assert(!capture_fair_final_phase_snapshot(&manager, 7, 13, 1200));
  manager.count = 1;
  write_field(restored, 0x78, 1);
  assert(!restore_fair_final_phase_snapshot(&manager, 7, 13, 1200, saved));
  write_field(restored, 0x78, 0);
  write_field(restored, 0x10, 0);
  assert(!restore_fair_final_phase_snapshot(&manager, 7, 13, 1200, saved));
  write_field(restored, 0x10, 1);
  assert(update_phase_cache({&manager, &manager, 7, 13, 1200}, &restored,
      [](auto &cache) { cache.last_deploy_scale_tick = 1201; }));
  assert(!capture_fair_final_phase_snapshot(&manager, 7, 13, 1200));
  reset_epoch(14);
  auto no_cache = capture_fair_final_phase_snapshot(&manager, 7, 14, 1200);
  assert(no_cache && !no_cache->objects[0].has_cache);
  reset_epoch(15);
  assert(restore_fair_final_phase_snapshot(&manager, 7, 15, 1200, no_cache));
  assert(!copy_phase_cache(7, 15, &restored, &copied));
  std::puts("phase snapshot checks passed");
}
