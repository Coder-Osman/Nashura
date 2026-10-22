'use strict';

// Diagnostic events only: never fed to the policy or public event cursor.
// All device timestamps use CLOCK_MONOTONIC, the clock used by Java nanoTime.
class LiveTimingCapture {
  constructor(lib, context) {
    this.lib = lib;
    this.context = context;
    this.sequence = 0;
    this.commandSequence = 0;
    this.commands = new Map();
    this.lastSyncSample = null;
    this.errors = new Set();
    this.clockBuffer = Memory.alloc(16);
    this.clock = new NativeFunction(Module.getGlobalExportByName('clock_gettime'),
      'int', ['int', 'pointer'], {scheduling: 'exclusive'});
    this.listeners = [];
  }

  now() {
    if (this.clock(1, this.clockBuffer) !== 0) throw new Error('CLOCK_MONOTONIC failed');
    const seconds = this.clockBuffer.readS64().toString();
    const nanos = this.clockBuffer.add(8).readS64().toNumber();
    if (seconds.startsWith('-') || nanos < 0 || nanos >= 1000000000)
      throw new Error('invalid monotonic timespec');
    // Decimal concatenation preserves all nanoseconds beyond JS's 2^53 limit.
    return seconds + String(nanos).padStart(9, '0');
  }

  error(stage, error) {
    const message = String(error);
    const key = stage + ':' + message;
    if (this.errors.has(key)) return;
    this.errors.add(key);
    send({event: 'timing_error', stage, message});
  }

  emit(stage, stamp, c, fields = {}) {
    send({event: 'latency_marker', schema: 'cr-live-timing.v1', sequence: ++this.sequence,
      stage, device_monotonic_ns: stamp, clock: 'android-clock-monotonic',
      battle_epoch: epoch, tick: c.tick, own_side: c.owner, ...fields});
  }

  syncState(c) {
    // Pinned build: +0x64 is the latest sync tick + 19, NOT a server ACK.
    const local = c.battle.add(0x60).readS32();
    const limit = c.battle.add(0x64).readS32();
    return {local_tick: local, sync_limit_tick: limit,
      last_sync_tick: c.battle.add(0x1bc).readS32(),
      ticks_ahead_of_limit: local - limit,
      stopped: c.battle.add(0x1e9).readU8(),
      stop_detail_flag: c.battle.add(0x1eb).readU8()};
  }

  observeBattle(c) {
    try {
      const state = this.syncState(c), stamp = this.now();
      const key = epoch + ':' + c.world.toString();
      const prior = this.lastSyncSample;
      // Emit transitions even when simulation ticks stop; otherwise at most 1 Hz.
      if (prior && prior.key === key && prior.stopped === state.stopped
          && prior.detail === state.stop_detail_flag
          && BigInt(stamp) - BigInt(prior.stamp) < 1000000000n) return;
      this.lastSyncSample = {key, stamp, stopped: state.stopped, detail: state.stop_detail_flag};
      this.emit('battle_sync_state', stamp, c, {battle_sync: state});
    } catch (error) { this.error('battle_sync_state', error); }
  }

  syncFault(battle, returnAddress, registers) {
    const c = this.context();
    if (!c || !untag(battle).equals(c.battle)) return;
    const offset = returnAddress.sub(this.lib.base).toUInt32();
    // All four stores set +0x1e9 before calling the shared report helper.
    // The helper is also called by a debug command without a new sync failure.
    const causes = {0x1063150: 'clock_lead_limit_exceeded',
      0x106454c: 'checksum_mismatch_during_step',
      0x1064fb8: 'checksum_mismatch_on_sync_update',
      0x1064708: 'explicit_out_of_sync'};
    const cause = causes[offset];
    if (!cause) return;
    const state = this.syncState(c);
    if (state.stopped !== 1) throw new Error('sync fault without stop flag');
    const fields = {cause, caller_return_offset: offset, battle_sync: state};
    if (offset === 0x106454c) {
      fields.checksum_tick = state.local_tick;
      fields.local_checksum = registers.x21.toUInt32();
      fields.received_checksum = registers.x24.toUInt32();
    } else if (offset === 0x1064fb8) {
      fields.checksum_tick = registers.x21.toInt32();
      fields.local_checksum = registers.x11.toUInt32();
      fields.received_checksum = registers.x20.toUInt32();
    }
    this.emit('battle_sync_fault', this.now(), c, fields);
  }

  localCommand(controller, reference) {
    const c = this.context();
    // This function is on the LOCAL outbound path. Verify its controller
    // before dereferencing the command, never inspect received enemy commands.
    if (!c || c.world.add(0x1e0).readU8() || !untag(controller).equals(c.account)) return null;
    const command = pointer(reference, 0);
    if (command.isNull()) return null;
    const vtable = pointer(command, 0);
    if (!pointer(vtable, 0x20).equals(this.lib.base.add(0xd31204))) return null;
    const packed = command.add(0x48).readU32();
    const slot = ((packed >>> 22) & 63) - 1;
    const deck = vector(pointer(c.world, 0x88 + c.owner * 8), 0x20, 16);
    if (deck.count !== 8 || slot < 0 || slot >= deck.count) throw new Error('own command deck slot');
    const card = pointer(deck.data, slot * 8);
    const cardId = pointer(card, 0x10).add(0x40).readU32();
    return {c, command, fields: {command_id: ++this.commandSequence, card_id: cardId,
      deck_slot: slot, cost: packed >>> 28, form_code: packed & 15,
      battle_sync: this.syncState(c),
      submit_state: {defer_enabled: untag(controller).add(0x328).readU8(),
        queued_commands: untag(controller).add(0x2c).readS32(),
        battle_stopped: c.battle.add(0x1e9).readU8()}}};
  }

  ownResources(c) {
    // f3bb18 uses floor(raw / 10000) minus the LOCAL reserved costs, not
    // simply the raw elixir shown by the simulation. Read no enemy queue.
    const pending = pointer(c.player, 0x248);
    const entries = pending.isNull() ? null : vector(pending, 0x10, 64);
    let reserved = 0;
    for (let i = 0; entries && i < entries.count; i++)
      reserved += pointer(entries.data, i * 8).add(0x10).readS32();
    return {elixir_raw: c.player.add(0x2f8).readS32(),
      reserved_elixir: reserved, pending_count: entries ? entries.count : 0};
  }

  install() {
    const timing = this;
    this.listeners.push(Interceptor.attach(this.lib.base.add(0x106482c), {
      onEnter(args) {
        try { timing.syncFault(args[0], this.returnAddress, this.context); }
        catch (error) { timing.error('battle_sync_fault', error); }
      },
    }));
    this.listeners.push(Interceptor.attach(this.lib.base.add(0xc893d0), {
      onEnter(args) {
        this.timedCommand = null;
        try {
          const stamp = timing.now();
          const found = timing.localCommand(args[0], args[1]);
          if (!found) return;
          // c893d0 receives a fully constructed own card command. This marks
          // creation COMPLETE / submission entry, not the allocator call.
          this.timedCommand = found;
          found.reference = args[1];
          found.outbound = false;
          found.validations = [];
          timing.commands.set(found.command.toString(), found);
          timing.emit('own_command_created', stamp, found.c, found.fields);
        } catch (error) { timing.error('own_command_created', error); }
      },
      onLeave(retval) {
        if (!this.timedCommand) return;
        const found = this.timedCommand;
        timing.commands.delete(found.command.toString());
        try {
          // The command may already have transferred ownership or been freed.
          // Read no command memory on return.
          const accepted = retval.toInt32() !== 0;
          // A zero return can also mean locally queued. Only the nullified
          // reference proves destruction; never treat zero alone as rejection.
          const destroyed = pointer(found.reference, 0).isNull();
          const rejected = !accepted && destroyed && !found.outbound
            && found.validations.length === 1 && found.validations[0].result !== 0;
          const state = found.fields.submit_state || {};
          const syncStopped = !accepted && destroyed && !found.outbound
            && found.validations.length === 0 && state.battle_stopped === 1
            && state.defer_enabled === 0 && state.queued_commands === 0;
          timing.emit('own_command_submit_returned', timing.now(), found.c,
            {...found.fields, accepted, command_destroyed: destroyed,
              outbound_seen: found.outbound, validations: found.validations,
              outcome: syncStopped ? 'battle_sync_stopped' : rejected ? 'validation_rejected' :
                found.outbound ? 'outbound' : accepted ? 'accepted_local' : 'unknown_or_queued'});
        } catch (error) { timing.error('own_command_submit_returned', error); }
      },
    }));
    this.listeners.push(Interceptor.attach(this.lib.base.add(0xd30a00), {
      onEnter(args) {
        this.validation = null;
        try {
          const found = timing.commands.get(untag(args[0]).toString());
          if (!found || !untag(args[1]).equals(found.c.battle)) return;
          const fields = {flags: args[2].toInt32(),
            native_position: [found.command.add(0x28).readS32(), found.command.add(0x2c).readS32()],
            ...timing.ownResources(found.c)};
          this.validation = {found, fields};
        } catch (error) { timing.error('own_command_validation', error); }
      },
      onLeave(retval) {
        if (!this.validation) return;
        try {
          const {found, fields} = this.validation;
          const result = {...fields, result: retval.toInt32()};
          found.validations.push(result);
          timing.emit('own_command_validated', timing.now(), found.c, {...found.fields, ...result});
        } catch (error) { timing.error('own_command_validation', error); }
      },
    }));
    this.listeners.push(Interceptor.attach(this.lib.base.add(0xc89778), {
      onEnter(args) {
        try {
          const stamp = timing.now();
          // Only a pointer already admitted by the verified LOCAL submit hook
          // is read here; opponent/network receive queues are never traversed.
          const command = pointer(args[1], 0);
          const found = timing.commands.get(command.toString());
          if (!found || !untag(args[0]).equals(found.c.account)) return;
          found.outbound = true;
          timing.emit('own_command_outbound', stamp, found.c,
            {...found.fields, command_tick: args[2].toInt32()});
        } catch (error) { timing.error('own_command_outbound', error); }
      },
    }));
  }
}
