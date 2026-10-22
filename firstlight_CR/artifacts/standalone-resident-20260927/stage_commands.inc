// Diagnostic snapshot digest: no filesystem I/O, same serializers as stage one.
bool handle_stage_command(int fd, const char *command) {
#ifdef CR_STANDALONE_HOST
  if (std::strncmp(command, "configure-native ", 17) == 0 ||
      std::strncmp(command, "advance-native ", 15) == 0 ||
      std::strncmp(command, "speed ", 6) == 0 ||
      std::strncmp(command, "standalone-trace ", 17) == 0) {
    send_control_error(fd, "this standalone host supports headless control only");
    return true;
  }
#endif
  if (std::strcmp(command, "state-digest") != 0) return false;
  pthread_mutex_lock(&g_control_mutex);
  while (g_controlled_step_active) pthread_cond_wait(&g_control_cond, &g_control_mutex);
  if (!g_controlled_manager || !g_control_gate_enabled || g_step_budget != 0) {
    pthread_mutex_unlock(&g_control_mutex);
    send_control_error(fd, "state digest requires a paused manager");
    return true;
  }
#ifndef CR_STANDALONE_HOST
  setenv("CR_SNAPSHOT_MODE", "original", 1);
#endif
  void *snapshot = battle_probe_snapshot_wrapper(g_libg_base, g_controlled_manager);
#ifndef CR_STANDALONE_HOST
  unsetenv("CR_SNAPSHOT_MODE");
#endif
  char digest[65] = {};
  const auto size = snapshot ? read_object_field<std::int32_t>(snapshot, 0x1c) : 0;
  const auto bytes = snapshot ? read_object_field<const void *>(snapshot, 0x40) : nullptr;
  const bool valid = bytes && size > 0 && size < 32 * 1024 * 1024 && sha256_bytes(bytes, size, digest);
  char response[256];
  std::snprintf(response, sizeof(response),
      "{\"ok\":%s,\"tick\":%d,\"bytes\":%d,\"sha256\":\"%s\"}",
      valid ? "true" : "false", g_game_state_tick(g_controlled_manager), size, digest);
  if (snapshot) g_state_snapshot_delete(snapshot);
  pthread_mutex_unlock(&g_control_mutex);
  send_control_response(fd, response);
  return true;
}
