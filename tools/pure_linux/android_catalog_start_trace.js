'use strict';

const events = [], coverage = [], errors = [];
let lost = 0;
let nextCall = 0;
let includeBatchCounts = false;
let includeBatchPhases = false;
const scopes = new Map();
const batchScopes = new Map();
const finishScopes = new Map();
const lookupScopes = new Map();
const innerScopes = new Map();
const insertionHistory = new Map();
const populationHistory = new Map();
const allocationHistory = new Map();
const pathCreationHistory = new Map();
const pathSourceHistory = new Map();
const sourceLoaderScopes = new Map();
const sourceParentHistory = new Map();
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
function nativeString16(p) {
  try {
    const object = p.and(ptr('0x00ffffffffffffff'));
    const length = object.add(4).readS32();
    if (length < 0 || length > 1024) return null;
    const data = length < 8 ? object.add(8) : object.add(8).readPointer().and(ptr('0x00ffffffffffffff'));
    const value = boundedString(data);
    return value !== null && value.length === length ? value : null;
  } catch (_) { return null; }
}
function lookupInput(p) {
  function readPointer(address) {
    const clean = address.and(ptr('0x00ffffffffffffff'));
    const range = Process.findRangeByAddress(clean);
    if (clean.isNull() || !range || !range.protection.includes('r') ||
        clean.add(8).compare(range.base.add(range.size)) > 0) throw new Error('lookup input unreadable');
    return clean.readPointer().and(ptr('0x00ffffffffffffff'));
  }
  const first = readPointer(p);
  return {first_nonnull: !first.isNull(), nested_nonnull: first.isNull() ? null : !readPointer(first.add(8)).isNull()};
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
function collectPath(module, history, args, returnAddress) {
  const mask = ptr('0x00ffffffffffffff');
  const owner = args[0].and(mask).readPointer().and(mask);
  if (owner.isNull()) throw new Error('null path owner');
  const key = owner.toString();
  if (!history.has(key)) {
    if (history.size >= 8192) throw new Error('path owner limit');
    history.set(key, new Map());
  }
  const items = history.get(key);
  const string = args[1].and(mask);
  const header = string.readU8();
  const length = (header & 1) ? string.add(8).readU64().toNumber() : header >>> 1;
  if (length > 4096) throw new Error('path length limit');
  const bytes = (header & 1) ? string.add(16).readPointer().and(mask) : string.add(1);
  const data = new Uint8Array(bytes.readByteArray(length + 1));
  if (data.length !== length + 1 || data[length] !== 0) throw new Error('path terminator missing');
  const caller = returnAddress.and(mask);
  const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
    ? caller.sub(module.base).toString() : 'external';
  const hex = Array.from(data.slice(0, length), byte => byte.toString(16).padStart(2, '0')).join('');
  const identity = label + ':' + hex;
  if (!items.has(identity)) {
    if (items.size >= 32768) throw new Error('unique path history limit');
    items.set(identity, {caller: label, bytes_hex: hex, calls: 0, returns: 0});
  }
  const item = items.get(identity);
  item.calls++;
  return item;
}
function observe(module) {
  if (module.name !== 'libg.so') return;
  if (includeBatchPhases) {
    attach(module, 0x11b4248, 'source_loader_candidate', {
      onEnter: function () {
        this.observerThread = Process.getCurrentThreadId();
        if (sourceLoaderScopes.has(this.observerThread)) errors.push('nested source loader');
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        this.label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        sourceLoaderScopes.set(this.observerThread, this.label);
      },
      onLeave: function () {
        if (sourceLoaderScopes.get(this.observerThread) !== this.label) errors.push('source loader scope mismatch');
        sourceLoaderScopes.delete(this.observerThread);
      }
    });
    attach(module, 0x11b0820, 'path_source_candidate', {
      onEnter: function (args) {
        try {
          this.pathItem = collectPath(module, pathSourceHistory, args, this.returnAddress);
          const owner = args[0].and(ptr('0x00ffffffffffffff')).readPointer().and(ptr('0x00ffffffffffffff')).toString();
          const parent = sourceLoaderScopes.get(Process.getCurrentThreadId()) || 'unobserved';
          if (!sourceParentHistory.has(owner)) sourceParentHistory.set(owner, {});
          const counts = sourceParentHistory.get(owner);
          counts[parent] = (counts[parent] || 0) + 1;
        }
        catch (error) { errors.push('path source: ' + String(error)); }
      },
      onLeave: function () { if (this.pathItem) this.pathItem.returns++; }
    });
    attach(module, 0x11afc60, 'path_creation_candidate', {
      onEnter: function (args) {
        try { this.pathItem = collectPath(module, pathCreationHistory, args, this.returnAddress); }
        catch (error) { errors.push('path creation: ' + String(error)); }
      },
      onLeave: function () { if (this.pathItem) this.pathItem.returns++; }
    });
    attach(module, 0x11b0084, 'tree_node_candidate', {
      onEnter: function (args) {
        const key = args[0].and(ptr('0x00ffffffffffffff')).toString();
        if (!allocationHistory.has(key)) {
          if (allocationHistory.size >= 8192) { errors.push('allocation history limit'); return; }
          allocationHistory.set(key, {calls: 0, returns: 0, callers: {}});
        }
        this.allocation = allocationHistory.get(key);
        this.allocation.calls++;
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        this.allocation.callers[label] = (this.allocation.callers[label] || 0) + 1;
      },
      onLeave: function () { if (this.allocation) this.allocation.returns++; }
    });
    attach(module, 0x11aebb0, 'tree_population_candidate', {
      onEnter: function (args) {
        const key = args[0].and(ptr('0x00ffffffffffffff')).toString();
        if (!populationHistory.has(key)) {
          if (populationHistory.size >= 8192) { errors.push('population history limit'); return; }
          populationHistory.set(key, {calls: 0, returns: 0, callers: {}});
        }
        this.population = populationHistory.get(key);
        this.population.calls++;
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        this.population.callers[label] = (this.population.callers[label] || 0) + 1;
      },
      onLeave: function () { if (this.population) this.population.returns++; }
    });
    attach(module, 0x11b2250, 'tree_insert_candidate', {
      onEnter: function (args) {
        const key = args[0].and(ptr('0x00ffffffffffffff')).toString();
        if (!insertionHistory.has(key)) {
          if (insertionHistory.size >= 8192) { errors.push('insertion history limit'); return; }
          insertionHistory.set(key, {calls: 0, returns: 0, callers: {}});
        }
        this.insertion = insertionHistory.get(key);
        this.insertion.calls++;
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        this.insertion.callers[label] = (this.insertion.callers[label] || 0) + 1;
      },
      onLeave: function () { if (this.insertion) this.insertion.returns++; }
    });
  }
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
          attach(module, 0x11ae1fc, 'lookup_inner', {
            onEnter: function (args) {
              this.observerThread = Process.getCurrentThreadId();
              this.observerBatch = lookupScopes.get(this.observerThread) || null;
              if (this.observerBatch) {
                if (innerScopes.has(this.observerThread)) errors.push('nested inner scope');
                innerScopes.set(this.observerThread, this.observerBatch);
                try {
                  const object = args[0].and(ptr('0x00ffffffffffffff'));
                  const insertion = insertionHistory.get(object.add(0xf8).toString());
                  const population = populationHistory.get(object.toString());
                  const allocation = allocationHistory.get(object.toString());
                  const paths = pathCreationHistory.get(object.toString());
                  const sourcePaths = pathSourceHistory.get(object.toString());
                  const sourceParents = sourceParentHistory.get(object.toString());
                  this.tree = {root_nonnull: !object.add(0x100).readPointer().isNull(),
                               size_word: object.add(0x108).readU64().toString(),
                               prior_insertion: insertion ? JSON.parse(JSON.stringify(insertion))
                                 : {calls: 0, returns: 0, callers: {}},
                               prior_population: population ? JSON.parse(JSON.stringify(population))
                                 : {calls: 0, returns: 0, callers: {}},
                               prior_allocation: allocation ? JSON.parse(JSON.stringify(allocation))
                                 : {calls: 0, returns: 0, callers: {}},
                               prior_paths: paths ? Array.from(paths.values(), item => ({...item})) : [],
                               prior_source_paths: sourcePaths ? Array.from(sourcePaths.values(), item => ({...item})) : [],
                               source_parents: sourceParents ? {...sourceParents} : {}};
                } catch (error) { errors.push('lookup tree: ' + String(error)); }
              }
            },
            onLeave: function (value) {
              if (this.observerBatch) {
                record({kind: 'lookup_inner', batch: this.observerBatch, nonnull: !value.isNull(), tree: this.tree});
                if (innerScopes.get(this.observerThread) !== this.observerBatch) errors.push('inner scope mismatch');
                innerScopes.delete(this.observerThread);
              }
            }
          });
          attach(module, 0x11b20f0, 'first_lookup_result', {
            onEnter: function (args) {
              this.observerBatch = innerScopes.get(Process.getCurrentThreadId()) || null;
              if (this.observerBatch) this.sentinel = args[0].and(ptr('0x00ffffffffffffff')).add(8);
            },
            onLeave: function (value) {
              if (!this.observerBatch) return;
              const result = value.and(ptr('0x00ffffffffffffff'));
              if (result.isNull()) { errors.push('null first lookup result'); return; }
              record({kind: 'first_lookup_result', batch: this.observerBatch,
                      found: !result.equals(this.sentinel)});
            }
          });
          attach(module, 0x1207ffc, 'finish_lookup', {
            onEnter: function (args) {
              this.observerBatch = finishScopes.get(Process.getCurrentThreadId()) || null;
              if (this.observerBatch) {
                this.observerThread = Process.getCurrentThreadId();
                if (lookupScopes.has(this.observerThread)) errors.push('nested lookup scope');
                lookupScopes.set(this.observerThread, this.observerBatch);
                this.observerKey = nativeString16(args[1]);
                try { this.observerInput = lookupInput(args[0]); }
                catch (error) { errors.push('lookup input: ' + String(error)); }
              }
            },
            onLeave: function (value) {
              if (this.observerBatch) {
                record({kind: 'finish_lookup', batch: this.observerBatch,
                        nonnull: !value.isNull(), key: this.observerKey, lookupInput: this.observerInput});
                if (lookupScopes.get(this.observerThread) !== this.observerBatch) errors.push('lookup scope mismatch');
                lookupScopes.delete(this.observerThread);
              }
            }
          });
          for (const [label, offset] of [['finish_a', 0xe1c290], ['finish_b', 0xdcd238]]) {
            attach(module, offset, label, {
              onEnter: function (args) {
                this.observerBatch = batchScopes.get(Process.getCurrentThreadId()) || null;
                if (!this.observerBatch) return;
                this.observerContext = args[0];
                this.observerThread = Process.getCurrentThreadId();
                if (label === 'finish_a') {
                  if (finishScopes.has(this.observerThread)) errors.push('nested finish scope');
                  finishScopes.set(this.observerThread, this.observerBatch);
                }
                const event = {kind: 'batch_phase', phase: label, edge: 'before', batch: this.observerBatch};
                try {
                  event.tableCounts = tableCounts(this.observerContext);
                  if (label === 'finish_a') {
                    const root = this.observerContext.and(ptr('0x00ffffffffffffff'));
                    const slot = root.add(0xb60);
                    const range = Process.findRangeByAddress(slot);
                    if (!range || !range.protection.includes('r') || slot.add(8).compare(range.base.add(range.size)) > 0)
                      throw new Error('context slot unreadable');
                    event.contextSlotB60Nonnull = !slot.readPointer().isNull();
                  }
                }
                catch (error) { errors.push(label + ' before: ' + String(error)); }
                record(event);
              },
              onLeave: function () {
                if (!this.observerBatch) return;
                const event = {kind: 'batch_phase', phase: label, edge: 'after', batch: this.observerBatch};
                try { event.tableCounts = tableCounts(this.observerContext); }
                catch (error) { errors.push(label + ' after: ' + String(error)); }
                if (label === 'finish_a') {
                  if (finishScopes.get(this.observerThread) !== this.observerBatch) errors.push('finish scope mismatch');
                  finishScopes.delete(this.observerThread);
                }
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
    activeScopes: scopes.size, activeBatchScopes: batchScopes.size, activeFinishScopes: finishScopes.size,
    activeLookupScopes: lookupScopes.size, activeInnerScopes: innerScopes.size,
    activeSourceLoaderScopes: sourceLoaderScopes.size}; }
};
