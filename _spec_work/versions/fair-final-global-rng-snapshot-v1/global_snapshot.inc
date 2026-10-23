constexpr std::uintptr_t kFairFinalGlobalMtOffset = 0x19fc368;
constexpr std::size_t kFairFinalGlobalMtSize = 0x9c8;
constexpr const char *kFairFinalGlobalMtLibgSha =
    "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783";

bool fair_final_global_single_lane(void *manager);

struct FairFinalGlobalRngSnapshot {
  std::uintptr_t libg_base = 0;
  void *manager = nullptr;
  std::uint64_t generation = 0;
  pid_t process = 0;
  std::uint8_t bytes[kFairFinalGlobalMtSize] = {};
  char sha256[65] = {};
};

bool fair_final_global_address(void *manager, std::uint8_t **address) {
  struct LibraryIdentity {
    std::uintptr_t base;
    bool accepted;
  };
  static const LibraryIdentity identity = []() {
    char digest[65] = {};
    return LibraryIdentity{g_libg_base,
        g_libg_base != 0 && sha256_file(g_libg_path, digest) &&
        std::strcmp(digest, kFairFinalGlobalMtLibgSha) == 0};
  }();
  if (address == nullptr || !identity.accepted || identity.base != g_libg_base ||
      g_libg_base > UINTPTR_MAX - kFairFinalGlobalMtOffset || !fair_final_global_single_lane(manager)) return false;
  auto *candidate = reinterpret_cast<std::uint8_t *>(g_libg_base + kFairFinalGlobalMtOffset);
  if (!libg_address_has_segment_flags(candidate, kFairFinalGlobalMtSize, PF_R | PF_W)) return false;
  *address = candidate;
  return true;
}

bool capture_fair_final_global_rng(void *manager, std::uint64_t generation, FairFinalGlobalRngSnapshot *saved) {
  std::uint8_t *address = nullptr;
  if (saved == nullptr || generation == 0 || !fair_final_global_address(manager, &address)) return false;
  FairFinalGlobalRngSnapshot captured;
  captured.libg_base = g_libg_base;
  captured.manager = manager;
  captured.generation = generation;
  captured.process = getpid();
  std::memcpy(captured.bytes, address, sizeof(captured.bytes));
  if (!sha256_bytes(captured.bytes, sizeof(captured.bytes), captured.sha256)) return false;
  *saved = captured;
  return true;
}

bool validate_fair_final_global_rng(void *manager, std::uint64_t generation,
    const FairFinalGlobalRngSnapshot &saved) {
  std::uint8_t *address = nullptr;
  char digest[65] = {};
  return generation != 0 && saved.generation == generation && saved.manager == manager &&
      saved.libg_base == g_libg_base && saved.process == getpid() &&
      fair_final_global_address(manager, &address) &&
      sha256_bytes(saved.bytes, sizeof(saved.bytes), digest) &&
      std::memcmp(digest, saved.sha256, sizeof(digest)) == 0;
}

bool restore_fair_final_global_rng(void *manager, std::uint64_t generation,
    const FairFinalGlobalRngSnapshot &saved) {
  std::uint8_t *address = nullptr;
  if (!validate_fair_final_global_rng(manager, generation, saved) ||
      !fair_final_global_address(manager, &address)) return false;
  std::memcpy(address, saved.bytes, sizeof(saved.bytes));
  return std::memcmp(address, saved.bytes, sizeof(saved.bytes)) == 0;
}

bool fair_final_global_digest(void *manager, const void *payload, std::size_t size, char digest[65]) {
  std::uint8_t *address = nullptr;
  if (payload == nullptr || size == 0 || size >= 32 * 1024 * 1024 || digest == nullptr ||
      !fair_final_global_address(manager, &address)) return false;
  constexpr char domain[] = "fair-final-state-with-global-mt.v1";
  std::uint8_t length[8] = {};
  for (std::size_t index = 0; index < sizeof(length); ++index)
    length[index] = static_cast<std::uint8_t>(static_cast<std::uint64_t>(size) >> (index * 8));
  Sha256Context context;
  sha256_init(&context);
  sha256_update(&context, reinterpret_cast<const std::uint8_t *>(domain), sizeof(domain) - 1);
  sha256_update(&context, length, sizeof(length));
  sha256_update(&context, static_cast<const std::uint8_t *>(payload), size);
  sha256_update(&context, address, kFairFinalGlobalMtSize);
  std::uint8_t result[32] = {};
  sha256_finish(&context, result);
  bytes_to_hex(result, sizeof(result), digest, 65);
  return true;
}
