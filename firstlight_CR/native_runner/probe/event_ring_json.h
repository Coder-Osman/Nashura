#pragma once

#include "probe_json.h"
#include "epoch_ring.h"
#include "observation_delta.h"

struct EventRingDescriptor {
  const char *schema;
  const char *source;
  const char *validation;
};

template <typename Snapshot>
bool append_event_ring_header(JsonWriter &json, const Snapshot &state, const EventRingDescriptor &descriptor,
                              std::uint64_t generation, std::uint64_t state_epoch, std::int32_t tick,
                              std::size_t capacity, bool attested, bool installed, std::uint64_t transmit_from) {
  json.begin_object();
  json.field("ok", true);
  json.field("schema", descriptor.schema);
  json.field("generation", generation);
  json.field("stateEpoch", state_epoch);
  json.field("observationTick", tick);
  json.field("capacity", capacity);
  json.field("hookSetAttested", attested);
  json.field("hookSetInstalled", installed);
  json.append(",\"capability\":{");
  json.field("status", installed && state.identity ? "derived" : "unavailable");
  json.field("source", descriptor.source);
  json.field("validation", descriptor.validation);
  json.field("confidence", "high");
  json.field("failClosed", true);
  json.end_object();
  json.field("epochFirstSequence", state.first);
  json.field("oldestRetainedSequence", state.oldest);
  json.field("transmitFromSequence", transmit_from);
  json.field("nextSequence", state.next);
  json.field("overflowCount", state.overflow);
  json.field("rejectedCount", state.rejected);
  json.field("sequenceGapBeforeOldest", state.oldest > state.first);
  json.field("complete", installed && state.identity && state.rejected == 0);
  json.begin_array("events");
  return json.good();
}

template <typename Record, std::size_t Capacity, typename Encoder>
bool append_epoch_ring_events(const EpochRing<Record, Capacity> &ring, const EventRingDescriptor &descriptor,
                              std::uint64_t generation, std::uint64_t state_epoch, std::int32_t tick, bool attested,
                              bool installed, char *response, std::size_t response_size, std::size_t *used,
                              std::uint64_t requested_from, Encoder encode) {
  const auto state = ring.snapshot(generation, state_epoch);
  std::uint64_t transmit_from = 0;
  if (!observation_event_start(requested_from, state.oldest, state.next, &transmit_from)) return false;
  JsonWriter json(response, response_size, used);
  if (!append_event_ring_header(json, state, descriptor, generation, state_epoch, tick, Capacity, attested,
                                installed, transmit_from)) {
    return false;
  }
  for (auto sequence = transmit_from; state.identity && sequence < state.next; ++sequence) {
    Record record;
    if (!ring.read(sequence, generation, state_epoch, tick, record) ||
        !encode(json, record, sequence == transmit_from)) {
      return false;
    }
  }
  return json.append("]}");
}
