// Called synchronously after the original content loader finishes on real ART.
extern "C" __attribute__((visibility("default")))
int cr_standalone_serve(JavaVM *vm, const char *root, int port) {
  if (!vm || !root || port < 1024 || port > 65535) return 50;
  g_java_vm = vm;
  const auto base = find_libg_base();
  if (!base) return 51;
  const int a = std::snprintf(g_user_fingerprint_path, sizeof(g_user_fingerprint_path),
      "%s/resources/runtime-update/fingerprint.json", root);
  const int b = std::snprintf(g_user_assets_path, sizeof(g_user_assets_path),
      "%s/resources/runtime-update/assets.scdb", root);
  if (a < 0 || a >= static_cast<int>(sizeof(g_user_fingerprint_path)) ||
      b < 0 || b >= static_cast<int>(sizeof(g_user_assets_path))) return 52;
  g_engine_instance_port = static_cast<std::uint16_t>(port);
  std::snprintf(g_engine_process_name, sizeof(g_engine_process_name), "standalone-resident");
  if (!install_hooks(base) || !inspect_content_runtime()) return 53;
  g_probe_runtime_started.store(true, std::memory_order_release);
  std::printf("{\"event\":\"standalone_resident_ready\",\"port\":%d,\"pid\":%d}\n", port, getpid());
  std::fflush(stdout);
  run_control_server(g_engine_instance_port);
  return 54; // Unexpected server exit is a failed process, never a ready daemon.
}
