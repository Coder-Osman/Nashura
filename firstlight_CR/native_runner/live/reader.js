'use strict';

// Exact-build layouts: supported_engine.json and cr_replay_probe.cpp.
// The manager/identity anchors were checked against cr-memory-reader,
// commit 9af94ca2f273ce526dda6256bdbd5d5555fb82d4.
// f38a68 observes a card already being consumed, including spells. It never
// inspects the opponent's hand, cycle, deck, elixir or pending commands.
const nil = ptr(0);
const untag = p => p.and('0x00ffffffffffffff');
const pointer = (p, off) => untag(p.add(off).readPointer());
const int = (p, off) => p.add(off).readS32();
const INT32_MAX = 0x7fffffff;
let lastWorld = nil, epoch = 0, sequence = 0;
let lastTick = -1, lastSent = 0, busy = false, listeners = [], active = false;
let selection;
let libBase, activeBattle = nil, eventStartTick = 0, plays = [], playError = null;
let phaseCapture, timingCapture;

function bindWorld(world, tick) {
  if (!active || !world.equals(lastWorld) || tick < lastTick) {
    epoch++; lastWorld = world; lastTick = -1;
    eventStartTick = tick; plays = []; playError = null;
    if (phaseCapture) phaseCapture.reset();
  }
  active = true;
}

function bounded(p, off, low, high) {
  const value = int(p, off);
  if (value < low || value > high)
    throw new Error('invalid public runtime field: offset=0x' + off.toString(16)
      + ' value=' + value + ' range=' + low + '..' + high);
  return value;
}

function phaseFacts(entity) {
  const attack = component(entity, 0), movement = component(entity, 1);
  // These are exact vtables from phase_runtime_layout.h. Other component
  // implementations remain unknown rather than being cast to this layout.
  if (attack.isNull() || !pointer(attack, 0).equals(libBase.add(0x18a1dd0))) return null;
  const data = pointer(entity, 0x48), target = pointer(attack, 0x10);
  const result = {
    // Nonnegative native int32 timers: helper HitSpeed can be 9,999,999 ms.
    attack_timeline_ms: bounded(attack, 0x24, 0, INT32_MAX),
    load_remaining_ms: bounded(attack, 0x28, 0, INT32_MAX),
    attack_sequence_stage: bounded(attack, 0x20, -1, 255),
    deploy_remaining_ms: bounded(entity, 0x15c, 0, INT32_MAX),
    deploy_previous_ms: bounded(entity, 0x160, 0, INT32_MAX),
    configured_deploy_time_ms: bounded(data, 0x490, 0, INT32_MAX),
    hit_speed_ms: bounded(data, 0x418, 0, INT32_MAX),
    attack_dash_time_ms: bounded(data, 0x6a0, 0, INT32_MAX),
    // Resolve this pointer against the visible object membership map before
    // serializing a target ID. Never dereference a possibly stale target.
    target_pointer: target.toString(),
    target_null: target.isNull(),
    movement_validated: !movement.isNull() && pointer(movement, 0).equals(libBase.add(0x18a1f38)),
    ...phaseCapture.snapshot(entity, lastTick),
  };
  if (result.movement_validated) {
    // 10000 is the ready threshold, not a cap: native steps may overshoot it.
    // Match valid_classic_charge_progress in phase_runtime_layout.h.
    result.classic_charge_progress = bounded(movement, 0x1e0, -1, INT32_MAX);
    result.charge_speed_multiplier = bounded(data, 0x43c, 0, INT32_MAX);
  }
  return result;
}

function vector(p, off, bound) {
  const data = pointer(p, off), capacity = int(p, off + 8), count = int(p, off + 12);
  if (count < 0 || capacity < count || capacity > bound || (count && data.isNull()))
    throw new Error('invalid bounded vector');
  return {data, count, capacity};
}

function verifyEntityCollection(collection, expected, members, identities) {
  const current = vector(collection, 8, 2048);
  if (!current.data.equals(expected.data) || current.count !== expected.count
      || current.capacity !== expected.capacity)
    throw new Error('entity collection changed during read: count=' + expected.count + '->' + current.count);
  for (let i = 0; i < members.length; i++) {
    if (!pointer(current.data, i * 8).equals(members[i]))
      throw new Error('entity collection changed during read: index=' + i);
  }
  const seen = new Map();
  for (const entry of identities) {
    const prior = seen.get(entry.id);
    if (prior) throw new Error('duplicate entity identity: ' + JSON.stringify({
      id: entry.id, first_index: prior.index, second_index: entry.index,
      first_pointer: prior.pointer, second_pointer: entry.pointer,
      first_name: prior.name, second_name: entry.name}));
    seen.set(entry.id, entry);
  }
}

function retryableConsistencyError(message) {
  return /entity collection (changed during read|replaced during read)|duplicate entity identity:/.test(message);
}

function readWithConsistencyRetries(reader, maxAttempts) {
  let lastError = null;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      return reader();
    } catch (error) {
      lastError = error;
      const message = String(error);
      if (!retryableConsistencyError(message) || attempt + 1 === maxAttempts) throw error;
    }
  }
  throw lastError;
}

function component(entity, index) {
  const list = vector(entity, 0x18, 64);
  if (list.count <= index) return nil;
  const result = pointer(list.data, index * 8);
  if (!result.isNull() && !pointer(result, 8).equals(entity))
    throw new Error('component owner mismatch');
  return result;
}

function dataName(data) {
  const length = int(data, 0x2c);
  if (length <= 0 || length > 128) throw new Error('invalid public data name');
  return (length < 8 ? data.add(0x30) : pointer(data, 0x30)).readUtf8String(length);
}

function ownCards(world, player, owner) {
  const deck = vector(pointer(world, 0x88 + owner * 8), 0x20, 16);
  if (deck.count !== 8) throw new Error('only eight-card 1v1 decks are supported');
  function card(index) {
    if (index === -1) return null;
    if (index < 0 || index >= deck.count) throw new Error('invalid deck index');
    const slot = pointer(deck.data, index * 8), data = pointer(slot, 0x10);
    const selected = selection(slot, player, 0), packed = selected[2];
    if (!untag(selected[0]).equals(data) || ((packed >>> 22) & 63) - 1 !== index)
      throw new Error('own card selection identity mismatch');
    return {deck_slot: index, card_id: int(data, 0x40), form_code: packed & 15, cost: packed >>> 28};
  }
  const hand = vector(player, 0x220, 8), cycle = vector(player, 0x230, 16);
  if (hand.count !== 4) throw new Error('visible hand must have four slots');
  return {
    deck: Array.from({length: deck.count}, (_, i) => card(i)),
    hand: Array.from({length: hand.count}, (_, i) => ({slot: i, card: card(int(hand.data, i * 4))})),
    next_card: cycle.count ? card(int(cycle.data, 0)) : null,
  };
}

function visibleEntity(entity, ownSide) {
  const owner = int(entity, 0x78), kind = int(entity, 0x30);
  if (![0, 1].includes(owner) || ![12, 13, 15].includes(kind)) return {omitted: 'unsupported'};
  const hp = component(entity, 2);
  if (hp.isNull()) return {omitted: 'unsupported'};
  // Validate the exact type-2 layout, not an arbitrary numerical HP ceiling.
  // Hero visual helpers legitimately have multi-million HP after scaling.
  const hpVtable = pointer(hp, 0);
  if (!hpVtable.equals(libBase.add(0x18a1ec0))
      || !pointer(hpVtable, 0x20).equals(libBase.add(0xf62f80)))
    throw new Error('invalid HP component type');
  const currentHp = int(hp, 0x10), maxHp = int(hp, 0x14);
  if (currentHp <= 0) return {omitted: 'dead'};
  const effects = component(entity, 3);
  let visibility = 'unknown';
  if (!effects.isNull()) {
    const count = int(effects, 0x24), capacity = int(effects, 0x20), invisible = int(effects, 0x34);
    if (count < 0 || capacity < count || capacity > 64 || invisible < 0 || invisible > count)
      throw new Error('invalid visibility state');
    visibility = invisible ? 'invisible' : 'visible';
  }
  // Live troops can have kind 12 as well as 15; kind is not a privacy gate.
  if (owner !== ownSide && visibility !== 'visible') return {omitted: 'visibility'};
  const data = pointer(entity, 0x48), dataId = data.add(0x40).readU32(), name = dataName(data);
  const objectId = entity.add(8).readU32();
  // Native HP fields are signed int32. A zero static cap is legal, as is
  // current HP above the static cap (e.g. the Evolution Witch heal path).
  if (maxHp < 0)
    throw new Error('invalid HP: entity=' + objectId + ' name=' + name + ' data_id=' + dataId
      + ' hp=' + currentHp + ' max_hp=' + maxHp);
  const x = int(entity, 0x7c), y = int(entity, 0x80);
  if (x < -1000 || x > 19000 || y < -1000 || y > 33000) throw new Error('invalid arena position');
  if (!objectId) throw new Error('zero entity identity');
  return {entity: {
    id: objectId, owner, kind, card_id: int(entity, 0xac),
    native_data_id: dataId,
    native_data_name: name,
    x, y, hp: currentHp, max_hp: maxHp, visibility,
    level_index: kind === 15 ? int(entity, 0x120) : null,
    phase: phaseFacts(entity),
  }};
}

rpc.exports = {
  start(config) {
    if (listeners.length) throw new Error('reader already started');
    const lib = Process.getModuleByName('libg.so');
    libBase = lib.base;
    if (Process.arch !== 'arm64') throw new Error('requires arm64');
    for (const anchor of config.anchors) {
      const address = lib.base.add(anchor.offset);
      const range = Process.findRangeByAddress(address);
      const actual = Array.from(new Uint8Array(address.readByteArray(anchor.hex.length / 2)))
        .map(v => v.toString(16).padStart(2, '0')).join('');
      if (!range || !range.protection.includes('x') || actual !== anchor.hex)
        throw new Error('runtime signature mismatch: ' + anchor.name);
    }
    const getTick = new NativeFunction(lib.base.add(0x1062bc0), 'int', ['pointer']);
    const getManager = new NativeFunction(lib.base.add(0xbb6508), 'pointer', []);
    const getBattle = new NativeFunction(lib.base.add(0xbb93e0), 'pointer', ['pointer']);
    const getAccountContext = new NativeFunction(lib.base.add(0xc85c28), 'pointer', []);
    const getLocalAccount = new NativeFunction(lib.base.add(0xc8a954), 'pointer', ['pointer', 'int', 'int']);
    selection = new NativeFunction(lib.base.add(0xe2d1bc), ['pointer', 'pointer', 'uint32'], ['pointer', 'pointer', 'int']);
    phaseCapture = new LivePhaseCapture(lib, getTick, () => activeBattle, () => active);
    function context() {
      const manager = untag(getManager());
      if (manager.isNull()) return null;
      const battle = untag(getBattle(manager));
      if (battle.isNull()) return null;
      const world = pointer(battle, 0xa8);
      if (world.isNull()) return null;
      const account = untag(getAccountContext());
      if (account.isNull()) return null;
      const local = untag(getLocalAccount(account, 0, 0));
      if (local.isNull()) return null;
      const owner = [0, 1].find(i => {
        const identity = pointer(world, 0x30 + i * 8);
        return !identity.isNull() && int(identity, 0) === int(local, 0x20)
          && int(identity, 4) === int(local, 0x24);
      });
      if (owner === undefined) return null;
      return {manager, battle, world, account, owner, player: pointer(world, 0xe0 + owner * 8), tick: getTick(battle)};
    }
    timingCapture = new LiveTimingCapture(lib, context);
    timingCapture.install();
    listeners.push(Interceptor.attach(lib.base.add(0xf38a68), {
      onEnter(args) {
        this.receipt = null;
        this.consumptionTiming = null;
        let stamp = null;
        try { stamp = timingCapture.now(); }
        catch (error) { timingCapture.error('own_native_consume_enter', error); }
        try {
          const c = context();
          if (!c || c.tick < 0 || c.world.add(0x1e0).readU8()) return;
          bindWorld(c.world, c.tick);
          activeBattle = c.battle;
          // Pointer equality identifies the actor; no opponent state is read.
          const actor = [0, 1].find(i => pointer(c.world, 0xe0 + i * 8).equals(untag(args[0])));
          if (actor === undefined) return;
          const descriptor = untag(args[1]), data = pointer(descriptor, 0);
          const packed = descriptor.add(0x10).readU32();
          const form = packed & 15, cost = packed >>> 28, slot = ((packed >>> 22) & 63)-1;
          if (data.isNull() || form > 5 || cost > 15 || slot < 0 || slot > 7)
            throw new Error('invalid consumed card descriptor');
          let cardId = data.add(0x40).readU32(), identity = 'consumed-descriptor';
          if (actor === c.owner) {
            cardId = ownCards(c.world, c.player, c.owner).deck[slot].card_id;
            identity = 'own-deck';
          }
          if (!cardId) throw new Error('missing consumed card identity');
          this.receipt = {tick: c.tick, owner: actor, card_id: cardId, cost, form_code: form,
            source: 'native-consume-card.v1', card_identity: identity};
          this.world = c.world; this.epoch = epoch;
          if (actor === c.owner && stamp !== null) {
            this.consumptionTiming = {stamp, c};
            try {
              timingCapture.emit('own_native_consume_enter', stamp, c,
                {card_id: cardId, deck_slot: slot, public_sequence: plays.length + 1});
            } catch (error) { timingCapture.error('own_native_consume_enter', error); }
          }
        } catch (error) { playError = String(error); }
      },
      onLeave() {
        if (!this.receipt || this.epoch !== epoch || !this.world.equals(lastWorld)) return;
        if (plays.length >= 2048) { playError = 'public event capacity exceeded'; return; }
        plays.push({sequence: plays.length + 1, ...this.receipt});
        if (this.consumptionTiming) {
          try {
            timingCapture.emit('own_native_consumed', timingCapture.now(), this.consumptionTiming.c,
              {card_id: this.receipt.card_id, public_sequence: plays.length});
          } catch (error) { timingCapture.error('own_native_consumed', error); }
        }
      },
    }));
    let lastError = '';
    listeners.push(setInterval(function sample() {
        const now = Date.now();
        if (busy || now - lastSent < config.interval_ms) return;
        busy = true;
        lastSent = now;
        try {
          const manager = untag(getManager());
          if (manager.isNull()) {
            if (active) send({event: 'idle'});
            active = false;
            return;
          }
          const battle = untag(getBattle(manager));
          if (battle.isNull()) return;
          const world = pointer(battle, 0xa8);
          if (world.isNull()) return;
          const context = untag(getAccountContext());
          if (context.isNull()) return;
          const local = untag(getLocalAccount(context, 0, 0));
          if (local.isNull()) return;
          // Match the full two-word public roster ID; never serialize identities
          // or dereference the opponent's private player state.
          const owner = [0, 1].find(i => {
            const identity = pointer(world, 0x30 + i * 8);
            return int(identity, 0) === int(local, 0x20) && int(identity, 4) === int(local, 0x24);
          });
          if (owner === undefined) throw new Error('local account is not a battle player');
          const player = pointer(world, 0xe0 + owner * 8);
          const tick = getTick(battle);
          if (tick < 0 || tick > 1000000) throw new Error('invalid battle tick');
          bindWorld(world, tick);
          activeBattle = battle;
          timingCapture.observeBattle({manager, battle, world, account: context, owner, player, tick});
          if (tick === lastTick) return;
          // Entity vectors can compact while a troop dies or spawns. Read the
          // whole frame again instead of publishing fields from two vector
          // generations. The retry is intentionally narrow: malformed native
          // fields still fail immediately, while collection races get a few
          // chances to settle within this capture tick.
          const frame = readWithConsistencyRetries(() => {
              const collection = pointer(player, 0x10), objects = vector(collection, 8, 2048);
              const members = Array.from({length: objects.count}, (_, i) => pointer(objects.data, i * 8));
              const entities = [], identities = [], visiblePointers = new Map();
              const omitted = {unsupported: 0, dead: 0, visibility: 0};
              for (let i = 0; i < objects.count; i++) {
                const entity = members[i];
                if (entity.isNull()) continue;
                const row = visibleEntity(entity, owner);
                if (row.omitted) { omitted[row.omitted]++; continue; }
                identities.push({id: row.entity.id, index: i, pointer: entity.toString(),
                  name: row.entity.native_data_name});
                entities.push(row.entity);
                visiblePointers.set(entity.toString(), row.entity.id);
              }
              for (const entity of entities) {
                if (!entity.phase) continue;
                const p = entity.phase;
                p.target_id = visiblePointers.get(p.target_pointer) ?? null;
                p.target_validated = p.target_null || p.target_id !== null;
                delete p.target_pointer; delete p.target_null;
              }
              const rawElixir = int(player, 0x2f8);
              if (rawElixir < 0 || rawElixir > 100000) throw new Error('invalid own elixir');
              const ended = world.add(0x1e0).readU8() !== 0;
              const own = {...ownCards(world, player, owner), elixir_raw: rawElixir};
              if (getTick(battle) !== tick || !pointer(battle, 0xa8).equals(world)
                  || !untag(getManager()).equals(manager) || !untag(getBattle(manager)).equals(battle)) return;
              if (!pointer(player, 0x10).equals(collection))
                throw new Error('entity collection replaced during read');
              verifyEntityCollection(collection, objects, members, identities);
              if (getTick(battle) !== tick) return;
              return {entities, omitted, own, ended};
          }, 3);
          if (!frame) return;
          const {entities, omitted, own, ended} = frame;
          // Only suppress this tick after a complete, internally consistent frame.
          lastTick = tick;
          send({schema: 'cr-live-snapshot.v1', event: 'snapshot', sequence: ++sequence,
            battle_epoch: epoch, tick, captured_unix_ms: now, own_side: owner, ended,
            winner: ended ? int(world, 0x1b8) : null,
            own, consistency: 'same-tick-best-effort',
            entities, omitted,
            public_events: {schema: 'cr-live-public-events.v1', started_tick: eventStartTick,
              first_sequence: 1, next_sequence: plays.length + 1, complete: playError === null,
              events: plays},
            coverage: {base_entities: true, combat_phases: false, spells: false,
              public_card_plays: playError === null, attack_timers: true,
              equipped_forms: false, model_ready: false},
          });
          lastError = '';
        } catch (error) {
          const message = String(error);
          if (message !== lastError) send({event: 'read_error', message});
          lastError = message;
        } finally { busy = false; }
    }, config.interval_ms));
    return {event: 'ready', path: lib.path, interval_ms: config.interval_ms,
      timing_clock: 'android-clock-monotonic', device_monotonic_ns: timingCapture.now()};
  },
};
