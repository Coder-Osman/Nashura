struct FairFinalPhaseObject {
  NativeEntityIdentityView identity;
  bool has_deploy = false;
  std::int32_t deploy_remaining_ms = 0;
  std::int32_t deploy_previous_ms = 0;
  bool has_cache = false;
  PhaseRuntimeCachePayload cache;
};

struct FairFinalPhaseSnapshot {
  std::uint64_t generation = 0;
  std::uint64_t source_epoch = 0;
  std::int32_t tick = -1;
  std::size_t count = 0;
  FairFinalPhaseObject objects[kMaxObservedObjects];
};

bool fair_final_phase_objects(void *manager, void ***objects, std::int32_t *count) {
  return read_combat_object_vector(combat_object_manager_from_game_manager(manager), objects, count) &&
      *count >= 0 && *count <= kMaxObservedObjects &&
      (*count == 0 || process_range_is_readable(*objects, sizeof(void *) * *count));
}

bool fair_final_phase_cache_valid(const PhaseRuntimeCachePayload &cache, std::int32_t tick) {
  for (const auto sample_tick : {cache.last_attack_scale_tick, cache.last_movement_scale_tick,
       cache.last_deploy_scale_tick, cache.last_effective_speed_tick, cache.last_movement_tick}) {
    if (sample_tick < -1 || sample_tick > tick) return false;
  }
  return true;
}

std::shared_ptr<const FairFinalPhaseSnapshot> capture_fair_final_phase_snapshot(
    void *manager, std::uint64_t generation, std::uint64_t epoch, std::int32_t tick) {
  void **objects = nullptr;
  std::int32_t count = 0;
  if (tick < 0 || g_phase_runtime_generation.load(std::memory_order_acquire) != generation ||
      g_phase_runtime_state_epoch.load(std::memory_order_acquire) != epoch ||
      !fair_final_phase_objects(manager, &objects, &count)) return nullptr;
  auto saved = std::make_shared<FairFinalPhaseSnapshot>();
  saved->generation = generation;
  saved->source_epoch = epoch;
  saved->tick = tick;
  for (std::int32_t index = 0; index < count; ++index) {
    void *object = objects[index];
    if (object == nullptr) continue;
    FairFinalPhaseObject &entry = saved->objects[saved->count];
    if (!read_native_entity_identity(object, &entry.identity)) return nullptr;
    for (std::size_t earlier = 0; earlier < saved->count; ++earlier) {
      if (saved->objects[earlier].identity.native_object_id == entry.identity.native_object_id) return nullptr;
    }
    void *attack = nullptr;
    entry.has_deploy = phase_component_at(object, 0, &attack);
    if (entry.has_deploy) {
      if (!process_range_is_readable(object, cr_phase_runtime::kObjectDeployPreviousOffset + sizeof(std::int32_t)))
        return nullptr;
      entry.deploy_remaining_ms = read_object_field<std::int32_t>(object, cr_phase_runtime::kObjectDeployRemainingOffset);
      entry.deploy_previous_ms = read_object_field<std::int32_t>(object, cr_phase_runtime::kObjectDeployPreviousOffset);
      if (entry.deploy_remaining_ms < 0 || entry.deploy_remaining_ms > 3600000 ||
          entry.deploy_previous_ms < 0 || entry.deploy_previous_ms > 3600000) return nullptr;
    }
    PhaseRuntimeCache cache;
    entry.has_cache = copy_phase_cache(generation, epoch, object, &cache);
    if (entry.has_cache) {
      if (cache.native_object_id != entry.identity.native_object_id || !fair_final_phase_cache_valid(cache, tick))
        return nullptr;
      entry.cache = cache;
      entry.cache.object = 0;
    }
    ++saved->count;
  }
  return saved;
}

bool restore_fair_final_phase_snapshot(void *manager, std::uint64_t generation, std::uint64_t epoch,
    std::int32_t tick, const std::shared_ptr<const FairFinalPhaseSnapshot> &saved) {
  void **objects = nullptr;
  std::int32_t count = 0;
  if (!saved || saved->generation != generation || saved->tick != tick || saved->count > kMaxObservedObjects ||
      g_phase_runtime_generation.load(std::memory_order_acquire) != generation ||
      g_phase_runtime_state_epoch.load(std::memory_order_acquire) != epoch ||
      !fair_final_phase_objects(manager, &objects, &count)) return false;
  void *matched[kMaxObservedObjects] = {};
  std::size_t live = 0;
  for (std::int32_t index = 0; index < count; ++index) {
    void *object = objects[index];
    if (object == nullptr) continue;
    NativeEntityIdentityView identity;
    if (!read_native_entity_identity(object, &identity)) return false;
    std::size_t target = 0;
    while (target < saved->count && saved->objects[target].identity.native_object_id != identity.native_object_id)
      ++target;
    if (target == saved->count || matched[target] != nullptr) return false;
    const auto &entry = saved->objects[target];
    if (entry.identity.owner != identity.owner || entry.identity.object_index != identity.object_index ||
        entry.identity.secondary_index != identity.secondary_index) return false;
    void *attack = nullptr;
    if (entry.has_deploy != phase_component_at(object, 0, &attack)) return false;
    if (entry.has_deploy &&
        (!process_range_is_readable(object, cr_phase_runtime::kObjectDeployPreviousOffset + sizeof(std::int32_t)) ||
         read_object_field<std::int32_t>(object, cr_phase_runtime::kObjectDeployRemainingOffset) !=
             entry.deploy_remaining_ms)) return false;
    if (entry.has_cache && (!entry.cache.used || entry.cache.native_object_id != identity.native_object_id ||
        entry.cache.generation != saved->generation || entry.cache.state_epoch != saved->source_epoch ||
        !fair_final_phase_cache_valid(entry.cache, tick))) return false;
    matched[target] = object;
    ++live;
  }
  if (live != saved->count) return false;
  const CombatCaptureContext context{manager, combat_object_manager_from_game_manager(manager), generation, epoch, tick};
  for (std::size_t index = 0; index < saved->count; ++index) {
    const auto &entry = saved->objects[index];
    if (entry.has_cache && !update_phase_cache(context, matched[index], [&](PhaseRuntimeCache &cache) {
          static_cast<PhaseRuntimeCachePayload &>(cache) = entry.cache;
          cache.object = reinterpret_cast<std::uintptr_t>(matched[index]);
          cache.generation = generation;
          cache.state_epoch = epoch;
        })) return false;
  }
  for (std::size_t index = 0; index < saved->count; ++index) {
    const auto &entry = saved->objects[index];
    if (entry.has_deploy) {
      std::memcpy(static_cast<std::uint8_t *>(matched[index]) + cr_phase_runtime::kObjectDeployPreviousOffset,
          &entry.deploy_previous_ms, sizeof(entry.deploy_previous_ms));
    }
  }
  return true;
}
