'use strict';

let configuration = null;
let hostPhase = 'load';
let engine = null;
const stacks = new Map();
const nativeScopes = new Map();
const hooks = new Map();
const counts = new Map();
const coverage = [];
const errors = [];
const tables = new Set();
const tableEvidence = [];
let sequence = 0;

function location(address) {
  const module = Process.findModuleByAddress(address);
  return module ? module.name + '+' + address.sub(module.base) : address.toString();
}

function phase() {
  const stack = stacks.get(Process.getCurrentThreadId()) || [];
  return stack.length ? stack[stack.length - 1] : hostPhase;
}

function record(entry, invocation, args) {
  const current = phase();
  const thread = Process.getCurrentThreadId();
  const caller = location(invocation.returnAddress);
  let instruction = 'unknown';
  try { instruction = Instruction.parse(invocation.returnAddress.sub(4)).mnemonic; } catch (_) {}
  const scopes = (nativeScopes.get(thread) || []).slice();
  const key = [entry.address, current, hostPhase, scopes.join('>'), caller, instruction, thread].join('|');
  let row = counts.get(key);
  if (!row) {
    row = {target: entry.address, labels: entry.labels.slice(), phase: current, caller, thread,
      caller_instruction: instruction, count: 0, first_sequence: sequence++,
      samples: [], state_effect: 'unknown', label_semantics: 'candidate aliases for this target'};
    row.host_phase = hostPhase;
    row.native_scopes = scopes;
    row.attribution = scopes.length ? 'same-thread instrumented native scope' :
      'host command overlap only; no cross-thread causal attribution';
    counts.set(key, row);
  }
  row.count++;
  if (row.samples.length < configuration.sample_limit) {
    const sample = {arguments: [args[0].toString(), args[1].toString(), args[2].toString()]};
    if (configuration.capture_backtraces === true) {
      try { sample.backtrace = Thread.backtrace(invocation.context, Backtracer.ACCURATE).slice(0, 16).map(location); }
      catch (_) {}
    }
    const label = entry.labels.join('|');
    try {
      if (label.includes('AAssetManager_open')) sample.request = args[1].readUtf8String(256);
      if (label.includes('JNI.NewStringUTF')) sample.request = args[1].readUtf8String(256);
    } catch (_) {}
    row.samples.push(sample);
    invocation.sample = sample;
  }
}

function hook(address, label, options = {}) {
  const identity = address.toString();
  if (hooks.has(identity)) {
    const entry = hooks.get(identity);
    if (!entry.labels.includes(label)) entry.labels.push(label);
    return;
  }
  const entry = {address: location(address), labels: [label]};
  try {
    const range = Process.findRangeByAddress(address);
    if (!range || !range.protection.includes('x')) throw new Error('target is not executable');
    Interceptor.attach(address, {
      onEnter(args) {
        this.scopeThread = Process.getCurrentThreadId();
        if (options.track_scope || options.step) {
          const scopes = nativeScopes.get(this.scopeThread) || [];
          scopes.push(label);
          nativeScopes.set(this.scopeThread, scopes);
          this.trackedScope = true;
        }
        this.entryPhase = null;
        if (options.phase) this.entryPhase = options.phase;
        if (options.step) {
          const tick = args[0].add(configuration.tick_offset).readS32();
          this.entryPhase = tick === 0 ? 'first_tick' : tick < 1200 ? 'sustained_early' :
            tick < 3600 ? 'sustained_middle' : 'sustained_late_terminal';
          watchManager(args[0]);
        }
        if (this.entryPhase) {
          const thread = Process.getCurrentThreadId();
          const stack = stacks.get(thread) || [];
          stack.push(this.entryPhase);
          stacks.set(thread, stack);
        }
        record(entry, this, args);
        if (options.vm) watchVM(args[0]);
        if (options.env) this.environmentOutput = args[1];
        if (options.manager) this.manager = args[0];
        if (options.command) {
          try { watchObject(args[1].readPointer(), 'command', 13); }
          catch (error) { errors.push({label: 'command', error: String(error)}); }
        }
      },
      onLeave(result) {
        if (this.sample) this.sample.result = result.toString();
        if (this.environmentOutput && result.toInt32() === 0) {
          try { watchEnvironment(this.environmentOutput.readPointer()); }
          catch (error) { errors.push({label, error: String(error)}); }
        }
        if (this.manager) watchManager(this.manager);
        if (options.bootstrap) installTrampolines();
        if (this.entryPhase) stacks.get(Process.getCurrentThreadId()).pop();
        if (this.trackedScope) nativeScopes.get(this.scopeThread).pop();
      }
    });
    hooks.set(identity, entry);
    coverage.push({label, target: entry.address, status: 'attached'});
  } catch (error) {
    const instructions = [];
    try {
      let cursor = address;
      for (let index = 0; index < 4; index++) {
        const decoded = Instruction.parse(cursor);
        instructions.push({address: location(cursor), instruction: decoded.toString()});
        cursor = decoded.next;
      }
    } catch (_) {}
    errors.push({label, target: entry.address, error: String(error), instructions,
      count: null, coverage: 'uncovered; hook installation failed'});
    coverage.push({label, target: entry.address, status: 'failed'});
  }
}

function watchTable(table, label, slots, names, optionsFactory) {
  const key = table.toString() + ':' + label;
  if (tables.has(key)) return;
  tables.add(key);
  const evidence = {label, table: location(table), slots, targets: []};
  tableEvidence.push(evidence);
  for (let index = 0; index < slots; index++) {
    if (names && !names[index]) continue;
    try {
      const target = table.add(index * Process.pointerSize).readPointer();
      const range = Process.findRangeByAddress(target);
      if (range && range.protection.includes('x')) {
        evidence.targets.push({slot: index, target: location(target)});
        hook(target, label + '.' + (names ? names[index] : 'slot' + index),
          optionsFactory ? optionsFactory(index) : {});
      }
    } catch (error) { errors.push({label, slot: index, error: String(error)}); }
  }
}

function watchVM(vm) {
  watchTable(vm.readPointer(), 'VM', 8, {
    3: 'DestroyJavaVM', 4: 'AttachCurrentThread', 5: 'DetachCurrentThread',
    6: 'GetEnv', 7: 'AttachCurrentThreadAsDaemon'
  }, index => ({env: [4, 6, 7].includes(index)}));
}

function watchEnvironment(environment) {
  watchTable(environment.readPointer(), 'JNI', configuration.jni_names.length,
    configuration.jni_names);
}

function watchObject(object, label, slots) {
  try {
    if (!object.isNull()) watchTable(object.readPointer(), label, slots);
  } catch (error) { errors.push({label, error: String(error)}); }
}

function watchManager(manager) {
  for (const field of configuration.manager_fields) {
    try { watchObject(manager.add(field.offset).readPointer(), field.name, field.slots); }
    catch (error) { errors.push({label: field.name, error: String(error)}); }
  }
}

function installTrampolines() {
  const probe = Process.getModuleByName(configuration.probe_module);
  for (const entry of configuration.trampolines) {
    hook(probe.base.add(entry.pointer_offset).readPointer(), entry.name, entry.options);
  }
}

function added(module) {
  if (!configuration) return;
  for (const symbol of module.enumerateExports()) {
    if (symbol.type === 'function' && symbol.name.startsWith('AAsset')) {
      hook(symbol.address, symbol.name);
    }
    if (module.name === configuration.probe_module && symbol.name === configuration.bootstrap_export) {
      hook(symbol.address, 'probe.bootstrap', {phase: 'probe_bootstrap', bootstrap: true});
    }
  }
  if (module.name !== configuration.engine_module) return;
  engine = module;
  for (const entry of configuration.functions) {
    hook(module.base.add(entry.offset), entry.name, entry.options || {});
  }
  for (const entry of configuration.static_tables) {
    watchTable(module.base.add(entry.offset), entry.name, entry.slots);
  }
  send({event: 'engine_observer_ready', module: module.name});
}

rpc.exports = {
  configure(value) {
    if (configuration) throw new Error('configuration is immutable');
    configuration = value;
    Process.attachModuleObserver({onAdded: added});
    return true;
  },
  phase(value) { hostPhase = value; return true; },
  snapshot() {
    return {schema: 'e0-dependency-counts.v1', engine_loaded: engine !== null,
      attribution_version: 2,
      threads: configuration.enumerate_threads === false ? [] :
        Process.enumerateThreads().map(thread => ({id: thread.id, name: thread.name || null})),
      thread_inventory: configuration.enumerate_threads === false ?
        'not collected in process; external inventory required' : 'in-process enumeration',
      counts: Array.from(counts.values()), coverage, errors, tables: tableEvidence,
      scope: 'configured entry points and observed vtables; not exhaustive',
      state_effect: 'unknown; counters do not establish causality'};
  }
};
