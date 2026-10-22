// Cumulative diagnostics queried only after a persistent batch session closes.
// Timers are nested: ordinary/rich are included in capture. Build timings include
// JSON formatting; they must not be labelled pure engine or network time.
struct CaptureProfile {
  std::atomic<std::uint64_t> step_ns{0}, capture_ns{0}, ordinary_ns{0}, rich_ns{0};
  std::atomic<std::uint64_t> compress_ns{0}, send_ns{0}, entries{0};
} g_capture_profile;

struct CaptureTimer {
  std::atomic<std::uint64_t> &counter;
  std::int64_t start;
  explicit CaptureTimer(std::atomic<std::uint64_t> &value) : counter(value), start(process_monotonic_now_ns()) {}
  ~CaptureTimer() { counter.fetch_add(process_monotonic_now_ns() - start, std::memory_order_relaxed); }
};

void send_control_response(int socket_fd, const char *response);

void send_capture_profile(int socket_fd) {
  char response[768];
  std::snprintf(response, sizeof(response),
    "{\"ok\":true,\"version\":\"capture-profile.v1\",\"entries\":%llu,\"step_ns\":%llu,"
    "\"capture_ns\":%llu,\"ordinary_ns\":%llu,\"rich_ns\":%llu,\"compress_ns\":%llu,\"send_ns\":%llu}",
    static_cast<unsigned long long>(g_capture_profile.entries.load()),
    static_cast<unsigned long long>(g_capture_profile.step_ns.load()),
    static_cast<unsigned long long>(g_capture_profile.capture_ns.load()),
    static_cast<unsigned long long>(g_capture_profile.ordinary_ns.load()),
    static_cast<unsigned long long>(g_capture_profile.rich_ns.load()),
    static_cast<unsigned long long>(g_capture_profile.compress_ns.load()),
    static_cast<unsigned long long>(g_capture_profile.send_ns.load()));
  send_control_response(socket_fd, response);
}
