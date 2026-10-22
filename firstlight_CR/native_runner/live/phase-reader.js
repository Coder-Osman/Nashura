// Exact-build read-only phase edges from probe/phase_runtime_layout.h.
// Only facts belonging to visible snapshot members are serialized.
class LivePhaseCapture {
  constructor(lib, tick, battle, isActive) {
    this.lib = lib; this.tick = tick; this.battle = battle; this.isActive = isActive;
    this.reset();
    const attach = (offset, enter, leave) => {
      const capture = this;
      Interceptor.attach(lib.base.add(offset), {
        onEnter(args) {
          this.fact = null;
          try {
            if (!capture.isActive() || capture.battle().isNull()) return;
            this.caller = this.returnAddress.sub(lib.base).toUInt32();
            this.fact = enter.call(this, args);
            this.generation = capture.generation;
          } catch (error) { capture.error = String(error); }
        },
        onLeave(result) {
          if (!this.fact || this.generation !== capture.generation) return;
          try { leave.call(this, result, this.fact); }
          catch (error) { capture.error = String(error); }
        },
      });
    };
    const object = args => untag(args[0]);
    const componentOwner = args => pointer(untag(args[0]), 8);
    const source = (entity, extra = {}) => ({entity, id: entity.add(8).readU32(), ...extra});
    attach(0xf23110, function(args) {
      if (this.caller !== 0xf61a7c || untag(args[2]).isNull()) return null;
      return source(object(args));
    }, (_result, f) => this.edge(f, 'attack_start', 0xf23110));
    attach(0xf5f100, function(args) {
      if (![0xf61d10, 0xf61e34].includes(this.caller)) return null;
      return source(componentOwner(args));
    }, (result, f) => {
      if (result.toInt32() & 1) this.edge(f, 'attack_release', 0xf5f100, {success: true});
    });
    attach(0xf5c894, args => source(componentOwner(args), {
      component: untag(args[0]), before: int(untag(args[0]), 0x24),
    }), (_result, f) => {
      const after = int(f.component, 0x24);
      if (f.before > 0 && after === 0)
        this.edge(f, 'target_reset', 0xf5c894, {timeline_before: f.before, timeline_after: after});
    });
    attach(0xf5a2d4, args => {
      const entry = untag(args[1]), data = pointer(entry, 0x18);
      return source(componentOwner(args), {buff_global_id: data.add(0x40).readU32(),
        buff_remaining_ms: int(entry, 8), speed_multiplier: int(data, 0xe4),
        hit_speed_multiplier: int(data, 0xe0)});
    }, (_result, f) => this.edge(f, 'effect_apply', 0xf5a2d4, {
      buff_global_id: f.buff_global_id, buff_remaining_ms: f.buff_remaining_ms,
      speed_multiplier: f.speed_multiplier, hit_speed_multiplier: f.hit_speed_multiplier,
    }));
    attach(0xf5b4ac, function(args) {
      return this.caller === 0xf62030 ? source(componentOwner(args), {input: args[1].toInt32()}) : null;
    }, (result, f) => {
      const output = result.toInt32();
      this.cache(f, {attack_step: {tick: this.now(), value: output}});
      this.edge(f, 'attack_scale', 0xf5b4ac, {input_step: f.input, output_step: output});
    });
    attach(0xf5b3c8, function(args) {
      if (![0xf1abc0, 0xf66290].includes(this.caller)) return null;
      return source(componentOwner(args), {input: args[1].toInt32(), deploy: this.caller === 0xf1abc0});
    }, (result, f) => this.cache(f, {[f.deploy ? 'deploy_step' : 'movement_step']:
      {tick: this.now(), value: result.toInt32(), input: f.input}}));
    attach(0xf1d004, args => source(object(args)), (result, f) =>
      this.cache(f, {effective_speed: {tick: this.now(), value: result.toInt32()}}));
    attach(0xf68808, args => source(componentOwner(args), {delta: args[1].toInt32()}), (_result, f) =>
      this.cache(f, {movement_delta: {tick: this.now(), value: f.delta}}));
  }
  now() { return this.tick(this.battle()); }
  reset() {
    this.generation = (this.generation || 0)+1;
    this.records = new Map(); this.sequence = 0; this.error = null;
  }
  cache(fact, update) {
    if (!fact.id || ![0, 1].includes(int(fact.entity, 0x78))) return null;
    const key = fact.entity.toString();
    let record = this.records.get(key);
    if (!record || record.id !== fact.id) {
      if (this.records.size >= 4096) throw new Error('phase fact capacity exceeded');
      record = {id: fact.id, events: {}};
    }
    Object.assign(record, update);
    this.records.set(key, record);
    return record;
  }
  edge(fact, kind, hook, extra = {}) {
    const record = this.cache(fact, {});
    if (record) record.events[kind] = {sequence: ++this.sequence, tick: this.now(), kind,
      hook_offset: hook, ...extra};
  }
  snapshot(entity, tick) {
    if (this.error) return {hooks_attested: false, hook_error: this.error, events: []};
    const record = this.records.get(entity.toString());
    const result = {hooks_attested: true, events: []};
    if (!record || record.id !== entity.add(8).readU32()) return result;
    result.events = Object.values(record.events).sort((a,b) => a.sequence-b.sequence);
    for (const field of ['attack_step','deploy_step','movement_step','movement_delta','effective_speed']) {
      if (record[field]?.tick === tick) result[field] = record[field];
    }
    return result;
  }
}
