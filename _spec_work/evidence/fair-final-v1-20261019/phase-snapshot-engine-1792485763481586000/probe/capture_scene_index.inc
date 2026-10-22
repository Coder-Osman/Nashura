// Capture-local only: the caller holds the engine lock and verifies the atomic
// generation/stateEpoch/tick before returning. Never retain pointers across steps.
struct CaptureSceneIndex {
  struct Entry { const void *pointer; std::uint32_t id; std::int32_t slot; };
  void *const *objects = nullptr;
  std::int32_t count = -1;
  std::int32_t size = 0;
  bool valid = false;
  Entry entries[kMaxObservedObjects] = {};
  std::uint32_t ids[kMaxObservedObjects] = {};

  void build(void *const *source, std::int32_t length) {
    objects = source;
    count = length;
    size = 0;
    valid = true;
    for (std::int32_t slot = 0; slot < length; ++slot) {
      const void *pointer = source[slot];
      if (pointer == nullptr) continue;
      // Preserve the old fail-closed behavior even for unrelated unreadable IDs.
      if (!process_range_is_readable(pointer, 0x0c)) { valid = false; return; }
      const auto id = read_object_field<std::uint32_t>(pointer, 0x08);
      if (id == 0) { valid = false; return; }
      entries[size] = {pointer, id, slot};
      ids[size++] = id;
    }
    std::sort(entries, entries + size, [](const Entry &a, const Entry &b) {
      return reinterpret_cast<std::uintptr_t>(a.pointer) < reinterpret_cast<std::uintptr_t>(b.pointer);
    });
    std::sort(ids, ids + size);
  }

  const Entry *find(const void *pointer) const {
    if (!valid) return nullptr;
    const auto entry = std::lower_bound(entries, entries + size, pointer, [](const Entry &a, const void *b) {
      return reinterpret_cast<std::uintptr_t>(a.pointer) < reinterpret_cast<std::uintptr_t>(b);
    });
    if (entry == entries + size || entry->pointer != pointer ||
        (entry + 1 != entries + size && entry[1].pointer == pointer)) return nullptr;
    const auto range = std::equal_range(ids, ids + size, entry->id);
    return range.second - range.first == 1 ? entry : nullptr;
  }
};

thread_local CaptureSceneIndex *g_capture_scene_index = nullptr;
struct CaptureSceneIndexScope {
  CaptureSceneIndex index;
  CaptureSceneIndex *previous;
  CaptureSceneIndexScope() : previous(g_capture_scene_index) { g_capture_scene_index = &index; }
  ~CaptureSceneIndexScope() { g_capture_scene_index = previous; }
  CaptureSceneIndexScope(const CaptureSceneIndexScope &) = delete;
  CaptureSceneIndexScope &operator=(const CaptureSceneIndexScope &) = delete;
};

CaptureSceneIndex *capture_scene_index(void *const *objects, std::int32_t count) {
  if (g_capture_scene_index == nullptr || objects == nullptr || count < 0 || count > kMaxObservedObjects)
    return nullptr;
  auto *index = g_capture_scene_index;
  if (index->objects != objects || index->count != count) index->build(objects, count);
  return index;
}
