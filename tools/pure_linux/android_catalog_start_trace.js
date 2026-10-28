'use strict';

const events = [], coverage = [], errors = [];
let lost = 0;
let nextCall = 0;
let includeBatchCounts = false;
let includeBatchPhases = false;
const scopes = new Map();
const batchScopes = new Map();
const observedSlots = [9, 10, 22, 34, 35, 123, 133, 166, 173, 179, 187, 189, 191, 205, 212, 213, 214];
function record(event) {
  if (events.length >= 30000) { lost++; return; }
  event.sequence = events.length;
  event.thread = Process.getCurrentThreadId();
  events.push(event);
}
function boundedString(p) {
  if (p.isNull()) return null;
  try {
    let value = '';
    for (let index = 0; index < 1024; index++) {
      const byte = p.add(index).readU8();
      if (!byte) return value;
      if (byte > 127) return null;
      value += String.fromCharCode(byte);
    }
  } catch (_) { return null; }
  return null;
}
function engineString(p) {
  try {
    const object = p.and(ptr('0x00ffffffffffffff'));
    const tag = object.readU8();
    const length = tag & 1 ? object.add(8).readU64().toNumber() : tag >> 1;
    if (length < 0 || length > 1023) return null;
    const data = tag & 1 ? object.add(16).readPointer().and(ptr('0x00ffffffffffffff')) : object.add(1);
    const value = boundedString(data);
    return value !== null && value.length === length ? value : null;
  } catch (_) { return null; }
}
function tableCounts(context) {
  function readable(value, size) {
    const address = value.and(ptr('0x00ffffffffffffff'));
    const range = Process.findRangeByAddress(address);
    if (address.isNull() || !range || !range.protection.includes('r') ||
        address.add(size).compare(range.base.add(range.size)) > 0) throw new Error('unreadable catalog memory');
    return address;
  }
  const root = readable(context, 219 * 8);
  return observedSlots.map(slot => {
    const object = root.add(slot * 8).readPointer();
    if (object.isNull()) return [slot, -1];
    const table = readable(object, 0x90);
    const capacity = table.add(0x38).readS32(), count = table.add(0x3c).readS32();
    const owner = table.add(0x78).readPointer().and(ptr('0x00ffffffffffffff'));
    const kind = table.add(0x84).readS32();
    if (!owner.equals(root) || kind !== slot || count < 0 || capacity < count || capacity > 1000000)
      throw new Error('catalog table invariant failed');
    return [slot, count];
  });
}
function attach(module, offset, label, callbacks) {
  try {
    const address = module.base.add(offset);
    if (!Process.findRangeByAddress(address).protection.includes('x')) throw new Error('not executable');
    Interceptor.attach(address, callbacks);
    coverage.push({label: label, installed: true});
  } catch (error) { coverage.push({label: label, installed: false}); errors.push(label + ': ' + String(error)); }
}
function observe(module) {
  if (module.name !== 'libg.so') return;
  let getterHooksAttempted = false;
  attach(module, 0xe1bdcc, 'table_batch', {
    onEnter: function (args) {
      if (!getterHooksAttempted) {
        getterHooksAttempted = true;
        for (const [label, offset] of [['entry_type', 0xdcb6fc], ['entry_id', 0xdcb474]]) {
          attach(module, offset, label, {
            onEnter: function () { this.batch = batchScopes.get(Process.getCurrentThreadId()) || null; },
            onLeave: function (value) { if (this.batch) record({kind: label, batch: this.batch, value: value.toUInt32()}); }
          });
        }
        if (includeBatchPhases) {
          for (const [label, offset] of [['finish_a', 0xe1c290], ['finish_b', 0xdcd238]]) {
            attach(module, offset, label, {
              onEnter: function (args) {
                this.observerBatch = batchScopes.get(Process.getCurrentThreadId()) || null;
                if (!this.observerBatch) return;
                this.observerContext = args[0];
                const event = {kind: 'batch_phase', phase: label, edge: 'before', batch: this.observerBatch};
                try { event.tableCounts = tableCounts(this.observerContext); }
                catch (error) { errors.push(label + ' before: ' + String(error)); }
                record(event);
              },
              onLeave: function () {
                if (!this.observerBatch) return;
                const event = {kind: 'batch_phase', phase: label, edge: 'after', batch: this.observerBatch};
                try { event.tableCounts = tableCounts(this.observerContext); }
                catch (error) { errors.push(label + ' after: ' + String(error)); }
                record(event);
              }
            });
          }
        }
      }
      this.observerThreadId = Process.getCurrentThreadId();
      this.batchCall = ++nextCall;
      if (batchScopes.has(this.observerThreadId)) errors.push('nested batch');
      batchScopes.set(this.observerThreadId, this.batchCall);
      let listCount = null;
      try { listCount = args[1].and(ptr('0x00ffffffffffffff')).add(12).readS32(); } catch (_) {}
      const event = {kind: 'table_batch', call: this.batchCall, args: [2, 3, 4, 5, 6].map(i => args[i].toUInt32()), listCount: listCount,
        caller: this.returnAddress.sub(module.base).toString()};
      if (includeBatchCounts) {
        this.countsContext = args[0];
        try { event.tableCounts = tableCounts(this.countsContext); }
        catch (error) { errors.push('batch before: ' + String(error)); }
      }
      record(event);
    },
    onLeave: function () {
      if (includeBatchCounts) {
        const event = {kind: 'table_batch_end', call: this.batchCall};
        try { event.tableCounts = tableCounts(this.countsContext); }
        catch (error) { errors.push('batch after: ' + String(error)); }
        record(event);
      }
      if (batchScopes.get(this.observerThreadId) !== this.batchCall) errors.push('batch scope mismatch');
      batchScopes.delete(this.observerThreadId);
    }
  });
  attach(module, 0xdcd364, 'postprocess', {
    onEnter: function () { record({kind: 'postprocess', caller: this.returnAddress.sub(module.base).toString()}); }
  });
  attach(module, 0x12e03e4, 'filesystem_open', {
    onEnter: function (args) {
      this.path = engineString(args[1]);
      this.observerThreadId = Process.getCurrentThreadId();
      this.callId = ++nextCall;
      this.parent = scopes.get(this.observerThreadId) || null;
      scopes.set(this.observerThreadId, this.callId);
    },
    onLeave: function (value) {
      let vtable = null;
      if (!value.isNull()) {
        try {
          const table = value.and(ptr('0x00ffffffffffffff')).readPointer().and(ptr('0x00ffffffffffffff'));
          const owner = Process.findModuleByAddress(table);
          vtable = owner && owner.name === 'libg.so' ? table.sub(owner.base).toString() : 'external';
        } catch (_) { vtable = 'unreadable'; }
      }
      record({kind: 'filesystem_open', path: this.path, nonnull: !value.isNull(), vtable: vtable,
        call: this.callId, parent: this.parent});
      if (scopes.get(this.observerThreadId) !== this.callId) errors.push('filesystem scope mismatch');
      if (this.parent) scopes.set(this.observerThreadId, this.parent); else scopes.delete(this.observerThreadId);
    }
  });
  record({kind: 'engine_hooks_ready'});
}
rpc.exports = {
  install: function (batchCounts, batchPhases) {
    includeBatchCounts = batchCounts === true;
    includeBatchPhases = batchPhases === true;
    Process.attachModuleObserver({onAdded: observe});
    const android = Process.findModuleByName('libandroid.so');
    if (android) {
      const open = android.findExportByName('AAssetManager_open');
      if (open) {
        Interceptor.attach(open, {
          onEnter: function (args) { this.path = boundedString(args[1]); this.parent = scopes.get(Process.getCurrentThreadId()) || null; },
          onLeave: function (value) { record({kind: 'asset_open', path: this.path, parent: this.parent, nonnull: !value.isNull()}); }
        });
        coverage.push({label: 'asset_open', installed: true});
      }
    }
    return {coverage: coverage, errors: errors};
  },
  snapshot: function () { return {events: events, coverage: coverage, errors: errors, lost: lost,
    activeScopes: scopes.size, activeBatchScopes: batchScopes.size}; }
};
