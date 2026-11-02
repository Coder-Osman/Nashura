#pragma once
#include <cstdint>
#include <cstddef>
#include <limits>

// Request-owned cursors: combat, phase, visibility, remaining,
// special movement, action movement, character state. Never global client state.
struct ObservationEventCursor {
  std::uint64_t generation = 0;
  std::uint64_t epoch = 0;
  std::uint64_t next[7] = {};
  std::uint64_t floor(std::uint64_t current_generation, std::uint64_t current_epoch,
                      std::size_t ring) const {
    return generation == current_generation && epoch == current_epoch ? next[ring] : 0;
  }
};
inline bool observation_event_start(std::uint64_t requested, std::uint64_t oldest,
                                    std::uint64_t next, std::uint64_t *start) {
  *start = requested ? requested : oldest;
  // Never silently skip a missed retained window.
  return *start >= oldest && *start <= next;
}

inline bool parse_observation_cursor(const char *text, ObservationEventCursor *cursor) {
  if (!text || !cursor) return false;
  std::uint64_t values[9] = {};
  for (auto &value : values) {
    while (*text == ' ' || *text == '\t') ++text;
    if (*text < '0' || *text > '9') return false;
    do {
      const auto digit = static_cast<std::uint64_t>(*text - '0');
      if (value > (std::numeric_limits<std::uint64_t>::max() - digit) / 10) return false;
      value = value * 10 + digit;
      ++text;
    } while (*text >= '0' && *text <= '9');
    if (*text && *text != ' ' && *text != '\t') return false;
  }
  while (*text == ' ' || *text == '\t') ++text;
  if (*text) return false;
  cursor->generation = values[0]; cursor->epoch = values[1];
  for (std::size_t i = 0; i < 7; ++i) cursor->next[i] = values[i + 2];
  return true;
}
