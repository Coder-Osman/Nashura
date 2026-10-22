bool resident_condition_draw_probe(int socket_fd, std::int32_t slot_id, const char *command) {
  unsigned long long generation = 0, epoch = 0;
  std::int32_t tick = -1;
  int consumed = 0;
  if (std::sscanf(command, "%llu %llu %d%n", &generation, &epoch, &tick, &consumed) != 3 || consumed <= 0) {
    send_control_error(socket_fd, "belief-draw-probe requires generation epoch tick expected24 replacement24");
    return true;
  }
  const char *cursor = command + consumed;
  std::int32_t expected[24] = {}, replacement[24] = {};
  for (auto *array : {expected, replacement}) {
    for (int index = 0; index < 24; ++index) {
      consumed = 0;
      if (std::sscanf(cursor, " %d%n", &array[index], &consumed) != 1 || consumed <= 0) {
        send_control_error(socket_fd, "belief-draw-probe queue shape differs");
        return true;
      }
      cursor += consumed;
    }
    for (int owner = 0; owner < 2; ++owner) {
      unsigned int seen = 0;
      bool padding = false;
      for (int index = owner * 12; index < (owner + 1) * 12; ++index) {
        if (array[index] == -1 && index < owner * 12 + 4) continue;
        if (array[index] == -2 && index >= owner * 12 + 8) {
          padding = true;
          continue;
        }
        if (padding) {
          send_control_error(socket_fd, "belief-draw-probe invalid cycle padding");
          return true;
        }
        if (array[index] < 0 || array[index] >= 8 || (seen & (1U << array[index]))) {
          send_control_error(socket_fd, "belief-draw-probe requires eight distinct deck slots per owner");
          return true;
        }
        seen |= 1U << array[index];
      }
      if (seen != 255) {
        send_control_error(socket_fd, "belief-draw-probe missing deck slot");
        return true;
      }
    }
  }
  while (*cursor == ' ' || *cursor == '\t') ++cursor;
  if (*cursor != '\0' || !resident_bind_slot(slot_id) || g_native_worker_running) {
    send_control_error(socket_fd, "belief-draw-probe requires an idle resident execution lane");
    return true;
  }
  ResidentMatchSlot &slot = g_resident_slots[slot_id];
  if (slot.generation != generation || slot.state_epoch != epoch || slot.tick != tick || slot.ended) {
    send_control_error(socket_fd, "belief-draw-probe stale match identity");
    return true;
  }
  PlayerStateView players[2];
  void *world = read_object_field<void *>(slot.manager, 0xa8);
  for (int owner = 0; owner < 2; ++owner) {
    if (!read_player_state(world, owner, &players[owner]) || players[owner].hand.count != 4 ||
        players[owner].cycle.count < 4 || players[owner].cycle.count > 8 || players[owner].deck_slots.count != 8 ||
        std::memcmp(players[owner].hand.data, expected + owner * 12, 4 * sizeof(std::int32_t)) != 0 ||
        std::memcmp(players[owner].cycle.data, expected + owner * 12 + 4,
                    players[owner].cycle.count * sizeof(std::int32_t)) != 0) {
      send_control_error(socket_fd, "belief-draw-probe compare-and-swap mismatch");
      return true;
    }
    int holes = 0;
    for (int index = 0; index < 4; ++index) holes += replacement[owner * 12 + index] == -1;
    if (holes + 4 != players[owner].cycle.count) {
      send_control_error(socket_fd, "belief-draw-probe changes refill count");
      return true;
    }
    for (int index = 4; index < 12; ++index) {
      const bool padded = index >= 4 + players[owner].cycle.count;
      if ((expected[owner * 12 + index] == -2) != padded ||
          (replacement[owner * 12 + index] == -2) != padded) {
        send_control_error(socket_fd, "belief-draw-probe changes cycle length");
        return true;
      }
    }
    constexpr std::int32_t hog_cards[] = {26000021, 26000038, 26000014, 26000010,
                                         26000030, 27000000, 28000000, 28000011};
    unsigned int cards_seen = 0;
    for (int index = 0; index < 8; ++index) {
      CardStateView card;
      if (!read_card_state(players[owner], index, true, &card) || (card.card_parameter & 15U) != 0) {
        send_control_error(socket_fd, "belief-draw-probe requires base card forms");
        return true;
      }
      for (int card_index = 0; card_index < 8; ++card_index) {
        if (card.card_id == hog_cards[card_index]) cards_seen |= 1U << card_index;
      }
    }
    if (cards_seen != 255) {
      send_control_error(socket_fd, "belief-draw-probe requires fixed Hog decks");
      return true;
    }
  }
  for (int owner = 0; owner < 2; ++owner) {
    std::memcpy(players[owner].hand.data, replacement + owner * 12, 4 * sizeof(std::int32_t));
    std::memcpy(players[owner].cycle.data, replacement + owner * 12 + 4,
                players[owner].cycle.count * sizeof(std::int32_t));
  }
  resident_save_bound_slot();
  ++g_state_epoch;
  begin_runtime_telemetry_epoch(slot.manager, slot.generation, g_state_epoch);
  bind_combat_event_object_manager(slot.manager);
  restore_tower_troop_runtime_snapshot(slot.generation, g_state_epoch, tick,
                                      slot.tower_troop_runtimes, slot.tower_troop_runtime_count);
  resident_save_bound_slot();
  char response[512] = {};
  std::snprintf(response, sizeof(response),
                "{\"ok\":true,\"experimental\":true,\"generation\":%llu,\"stateEpoch\":%llu,\"tick\":%d}",
                static_cast<unsigned long long>(slot.generation),
                static_cast<unsigned long long>(slot.state_epoch), tick);
  send_control_response(socket_fd, response);
  return true;
}
