'use strict';

let configuration = null;
let hostPhase = 'load';
let nativeStore = null;
let storeApi = null;
let constructor = null;
let holder = null;
let activeScope = new Map();
const retained = [];
const events = [];
const errors = [];
const status = {installed: false, targeted_reads: 0, targeted_writes: 0, fallback: 0, targeted_fallback: 0, mismatches: 0};

function text(pointer) {
  const length = pointer.add(4).readU32();
  if (length > 4095) throw new Error('string length outside declared contract');
  const data = length < 8 ? pointer.add(8) : pointer.add(8).readPointer().and('0x00ffffffffffffff');
  if (data.add(length).readU8() !== 0) throw new Error('unterminated engine string');
  return length ? data.readUtf8String(length) : '';
}

function nativeString(value) {
  if (value.includes('\0')) throw new Error('embedded NUL outside declared contract');
  return Memory.allocUtf8String(value);
}

function caller(address) {
  const module = Process.findModuleByAddress(address);
  return module ? module.name + '+' + address.sub(module.base) : 'unknown';
}

function dispatch(keyPointer, valuePointer, returnAddress, write) {
  const thread = Process.getCurrentThreadId();
  const event = {operation: write ? 'write' : 'read', thread, phase: hostPhase,
    native_scope: activeScope.get(thread) || null, caller: caller(returnAddress)};
  events.push(event);
  try {
    event.key = text(keyPointer);
    const entry = configuration.entries[event.key];
    event.targeted = Boolean(entry);
    if (!entry || configuration.mode === 'fallback' || configuration.mode === 'original') {
      if (!entry && configuration.mode === 'strict-targeted' &&
          !Object.prototype.hasOwnProperty.call(configuration.excluded_keys || {}, event.key)) throw new Error('undeclared configuration key inside targeted constructor');
      event.exclusion = (configuration.excluded_keys || {})[event.key] || 'outside declared constructor closure';
      status.fallback++;
      if (entry) status.targeted_fallback++;
      event.route = 'original_fallback';
      return 0;
    }
    if (write) {
      event.value = text(valuePointer);
      if (storeApi.write(nativeStore, nativeString(event.key), nativeString(event.value)) !== 0) throw new Error('undeclared or invalid configuration write');
      status.targeted_writes++;
      if (configuration.mode === 'shadow') { event.route = 'shadow_write_original'; return 0; }
      event.route = 'native_write';
      return 1;
    }
    const output = Memory.alloc(4096);
    if (storeApi.read(nativeStore, nativeString(event.key), output, 4096) !== 0) throw new Error('declared read missing from native store');
    event.expected = output.readUtf8String();
    status.targeted_reads++;
    if (configuration.mode === 'shadow') {
      holder.invoke(keyPointer, valuePointer);
      event.actual = text(valuePointer);
      event.route = 'shadow_read_original';
      if (event.actual !== event.expected) { status.mismatches++; throw new Error('shadow configuration value differs'); }
    } else {
      constructor(valuePointer, output);
      event.actual = text(valuePointer);
      event.route = 'native_read';
      if (event.actual !== event.expected) throw new Error('engine string constructor changed output');
    }
    return 1;
  } catch (failure) {
    event.error = String(failure);
    errors.push(event.error);
    send({event: 'e2_contract_violation', detail: event});
    return -1;
  } finally {
    if (configuration.stream_events) send({event: 'e2_configuration_event', detail: event});
  }
}

function install(module) {
  if (module.name !== configuration.engine_module) return;
  if (status.installed) return;
  try {
    constructor = new NativeFunction(module.base.add(configuration.string_constructor), 'void', ['pointer', 'pointer']);
    const scope = module.base.add(configuration.configuration_constructor);
    retained.push(Interceptor.attach(scope, {
      onEnter() { const thread = Process.getCurrentThreadId(); activeScope.set(thread, (activeScope.get(thread) || 0) + 1); },
      onLeave() { const thread = Process.getCurrentThreadId(); const count = activeScope.get(thread) - 1; if (count) activeScope.set(thread, count); else activeScope.delete(thread); }
    }));
    const readCallback = new NativeCallback((key, output, source) => dispatch(key, output, source, false), 'int', ['pointer', 'pointer', 'pointer']);
    const writeCallback = new NativeCallback((key, value, source) => dispatch(key, value, source, true), 'int', ['pointer', 'pointer', 'pointer']);
    retained.push(readCallback, writeCallback);
    const make = (callback, read) => {
      const originalSlot = Memory.alloc(Process.pointerSize);
      const code = Memory.alloc(Process.pageSize);
      Memory.patchCode(code, Process.pageSize, region => {
        const writer = new Arm64Writer(region, {pc: code});
        writer.putPushRegReg('x0', 'x1');
        writer.putPushRegReg('x8', 'lr');
        writer.putMovRegReg('x2', 'lr');
        if (read) writer.putMovRegReg('x1', 'x8');
        writer.putLdrRegAddress('x16', callback); writer.putBlrReg('x16');
        writer.putTbnzRegImmLabel('w0', 31, 'fatal');
        writer.putCmpRegReg('w0', 'wzr');
        writer.putPopRegReg('x8', 'lr');
        writer.putPopRegReg('x0', 'x1');
        writer.putBCondLabel('eq', 'fallback'); writer.putRet();
        writer.putLabel('fallback'); writer.putLdrRegAddress('x16', originalSlot); writer.putLdrRegRegOffset('x16', 'x16', 0); writer.putBrReg('x16');
        writer.putLabel('fatal');
        writer.putLdrRegAddress('x16', Process.getModuleByName('libc.so').getExportByName('abort')); writer.putBrReg('x16');
        writer.flush();
      });
      retained.push(code, originalSlot);
      return {code, originalSlot};
    };
    const readStub = make(readCallback, true); const writeStub = make(writeCallback, false);
    const readTarget = module.base.add(configuration.read_bridge);
    const writeTarget = module.base.add(configuration.write_bridge);
    readStub.originalSlot.writePointer(Interceptor.replaceFast(readTarget, readStub.code));
    writeStub.originalSlot.writePointer(Interceptor.replaceFast(writeTarget, writeStub.code));
    const invokeCode = Memory.alloc(Process.pageSize);
    Memory.patchCode(invokeCode, Process.pageSize, region => {
      const writer = new Arm64Writer(region, {pc: invokeCode});
      writer.putMovRegReg('x8', 'x1'); writer.putLdrRegAddress('x16', readStub.originalSlot); writer.putLdrRegRegOffset('x16', 'x16', 0); writer.putBrReg('x16'); writer.flush();
    });
    retained.push(invokeCode);
    holder = {invoke: new NativeFunction(invokeCode, 'void', ['pointer', 'pointer'])};
    status.installed = true;
  } catch (failure) { errors.push(String(failure)); send({event: 'e2_install_failed', error: String(failure)}); }
}

rpc.exports = {
  configure(config) {
    if (configuration) throw new Error('immutable configuration');
    if (!['original', 'shadow', 'replacement', 'fallback', 'strict-targeted'].includes(config.mode)) throw new Error('unknown mode');
    configuration = config;
    const library = Module.load(config.store_path);
    const bind = (name, result, args) => new NativeFunction(library.getExportByName('e2_store_' + name), result, args);
    storeApi = {create: bind('create', 'pointer', []), add: bind('add', 'int', ['pointer','pointer','pointer','int']),
      seal: bind('seal', 'int', ['pointer']), read: bind('read', 'int', ['pointer','pointer','pointer','ulong']),
      write: bind('write', 'int', ['pointer','pointer','pointer']), metric: bind('metric', 'uint64', ['pointer','uint'])};
    nativeStore = storeApi.create();
    if (nativeStore.isNull()) throw new Error('native store allocation failed');
    for (const [key, entry] of Object.entries(config.entries)) {
      if (storeApi.add(nativeStore, nativeString(key), nativeString(entry.value), entry.writable ? 1 : 0) !== 0) throw new Error('invalid contract entry');
    }
    if (storeApi.seal(nativeStore) !== 0) throw new Error('native store seal failed');
    Process.attachModuleObserver({onAdded: install});
    return true;
  },
  phase(value) { hostPhase = value; return true; },
  snapshot() { return {schema: 'e2-config-replacement.v1', mode: configuration.mode, status, events, errors,
    store: {reads: storeApi.metric(nativeStore, 0).toString(), writes: storeApi.metric(nativeStore, 1).toString(), failures: storeApi.metric(nativeStore, 2).toString()},
    scope: configuration.scope, context_lifecycle: 'process-owned native store; close/free not exercised by engine; no cross-process persistence',
    no_vm_verified: false, android_free_verified: false}; }
};
