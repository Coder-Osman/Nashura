'use strict';

function createPNBridge(config, invoke, lifecycle, preloadedLibrary) {
  const coldInstall = config.install_at === 'first-native-bridge';
  if (Process.arch !== 'arm64' || config.read_bridge !== 21022348 || config.write_bridge !== 21018548 ||
      !Number.isInteger(config.string_constructor) || config.mode !== 'replacement' ||
      (config.install_at !== undefined && !['post-cold-ready', 'first-native-bridge'].includes(config.install_at)) ||
      (coldInstall && (!lifecycle || typeof lifecycle.entry !== 'function' || typeof lifecycle.pn !== 'function'))) {
    throw new Error('explicit PN bridge installation contract required');
  }
  const engine = Process.getModuleByName('libg.so');
  const library = preloadedLibrary || Module.load(config.library);
  const nativeBackend = config.codec_backend ? createPNNativeBackend(config.codec_backend, invoke, library) : null;
  const read = new NativeFunction(library.getExportByName('e21_bridge_read'), 'int', ['pointer', 'pointer', 'ulong', 'pointer']);
  const write = new NativeFunction(library.getExportByName('e21_bridge_write'), 'int', ['pointer', 'pointer', 'pointer']);
  const construct = new NativeFunction(engine.base.add(config.string_constructor), 'void', ['pointer', 'pointer']);
  const retained = [];
  const events = [];
  const errors = [];
  let installed = false;
  let retired = false;
  let phase = coldInstall ? 'cold-start' : 'post-cold-ready';
  let fallbacks = 0;
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const encode = value => {
    const binary = unescape(encodeURIComponent(value));
    let result = '';
    for (let index = 0; index < binary.length; index += 3) {
      const bits = (binary.charCodeAt(index) << 16) | ((binary.charCodeAt(index + 1) || 0) << 8) |
        (binary.charCodeAt(index + 2) || 0);
      result += alphabet[(bits >>> 18) & 63] + alphabet[(bits >>> 12) & 63] +
        (index + 1 < binary.length ? alphabet[(bits >>> 6) & 63] : '=') +
        (index + 2 < binary.length ? alphabet[bits & 63] : '=');
    }
    return result;
  };
  const decode = value => {
    if (value.length % 4 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
      throw new Error('backend base64 outside contract');
    }
    const bytes = [];
    for (let index = 0; index < value.length; index += 4) {
      const bits = (alphabet.indexOf(value[index]) << 18) | (alphabet.indexOf(value[index + 1]) << 12) |
        ((value[index + 2] === '=' ? 0 : alphabet.indexOf(value[index + 2])) << 6) |
        (value[index + 3] === '=' ? 0 : alphabet.indexOf(value[index + 3]));
      bytes.push((bits >>> 16) & 255);
      if (value[index + 2] !== '=') bytes.push((bits >>> 8) & 255);
      if (value[index + 3] !== '=') bytes.push(bits & 255);
    }
    return bytes;
  };
  const boundedText = pointer => {
    const length = pointer.add(4).readU32();
    if (length > 4095) throw new Error('PN string outside bounded contract');
    const data = length < 8 ? pointer.add(8) : pointer.add(8).readPointer().and('0x00ffffffffffffff');
    if (data.isNull() || data.add(length).readU8() !== 0) throw new Error('invalid PN string');
    const value = length ? data.readUtf8String(length) : '';
    if (value.includes('\0') || unescape(encodeURIComponent(value)).length !== length) throw new Error('PN UTF-8 mismatch');
    return value;
  };
  const backendCall = operation => {
    if (retired) throw new Error('retired PN backend');
    return invoke(operation);
  };
  const guarded = callback => (...args) => {
    try { return callback(...args); }
    catch (failure) { errors.push(String(failure)); return -10; }
  };
  const readCallback = new NativeCallback(guarded((context, store, output, capacity) => {
    if (store > 1 || Number(capacity) !== 4096) throw new Error('PN backend read ABI differs');
    const bytes = nativeBackend ? nativeBackend.read(store) : decode(backendCall('bridge-read:' + store));
    if (bytes.length > 4095 || bytes.includes(0)) throw new Error('PN backend read text differs');
    output.writeByteArray(bytes.concat([0]));
    return 0;
  }), 'int', ['pointer', 'uint', 'pointer', 'ulong']);
  const writeCallback = new NativeCallback(guarded((context, store, value) => {
    if (store > 1) throw new Error('PN backend store differs');
    if (nativeBackend) { nativeBackend.write(store, value.readUtf8String()); return 0; }
    if (backendCall('bridge-write:' + store + ':' + encode(value.readUtf8String())) !==
        'writer-method-returned; commit-observe-separately') throw new Error('PN writer receipt differs');
    return 0;
  }), 'int', ['pointer', 'uint', 'pointer']);
  const backupCallback = new NativeCallback(guarded(() => {
    if (backendCall('bridge-backup') !== 'backup-method-returned; delivery-unknown') throw new Error('PN backup receipt differs');
    return 0;
  }), 'int', ['pointer']);
  const backend = Memory.alloc(4 * Process.pointerSize);
  [ptr(0), readCallback, writeCallback, backupCallback].forEach((value, index) => backend.add(index * Process.pointerSize).writePointer(value));
  retained.push(backend, readCallback, writeCallback, backupCallback);
  const dispatch = (key, value, source, writing) => {
    try {
      if (retired) throw new Error('PN bridge retired');
      if (coldInstall) lifecycle.entry(writing);
      if (key.add(4).readU32() !== 8) { fallbacks++; return 0; }
      const keyBytes = new Uint8Array(key.add(8).readPointer().and('0x00ffffffffffffff').readByteArray(8));
      if ([80, 78, 95, 80, 79, 80, 85, 80].some((byte, index) => keyBytes[index] !== byte)) {
        fallbacks++; return 0;
      }
      if (boundedText(key) !== 'PN_POPUP') throw new Error('PN key encoding differs');
      if (events.length >= 1024) throw new Error('PN event budget exceeded');
      const row = {operation: writing ? 'write' : 'read', phase, thread: Process.getCurrentThreadId(),
        caller: source.compare(engine.base) >= 0 && source.compare(engine.base.add(engine.size)) < 0
          ? 'libg.so+' + source.sub(engine.base) : 'external-probe',
        route: nativeBackend ? 'native-state-machine-native-codec-java-xml' : 'native-state-machine-java-backend'};
      events.push(row);
      if (coldInstall) {
        row.module_base = engine.base.toString();
        row.return_site = source.toString();
        if (writing) row.input = boundedText(value);
        lifecycle.pn(row);
      }
      const receipt = Memory.alloc(20);
      const output = Memory.alloc(4096);
      row.status = writing ? write(backend, Memory.allocUtf8String(boundedText(value)), receipt)
        : read(backend, output, 4096, receipt);
      row.receipt = {reads: receipt.readU32(), writes: receipt.add(4).readU32(), changed: receipt.add(8).readU32(),
        backup_method_returns: receipt.add(12).readU32(), failure: receipt.add(16).readS32()};
      if (row.status !== 0) throw new Error('PN native transition failed: ' + row.status);
      if (!writing) {
        construct(value, output);
        if (coldInstall) row.result = output.readUtf8String();
      }
      return 1;
    } catch (failure) {
      errors.push(String(failure));
      send({event: 'pn_bridge_contract_violation', error: String(failure)});
      return -1;
    }
  };
  const make = writing => {
    const callback = new NativeCallback((key, value, source) => dispatch(key, value, source, writing), 'int',
      ['pointer', 'pointer', 'pointer']);
    const slot = Memory.alloc(Process.pointerSize);
    const code = Memory.alloc(Process.pageSize);
    Memory.patchCode(code, Process.pageSize, region => {
      const writer = new Arm64Writer(region, {pc: code});
      writer.putPushRegReg('x0', 'x1'); writer.putPushRegReg('x8', 'lr');
      writer.putMovRegReg('x2', 'lr');
      if (!writing) writer.putMovRegReg('x1', 'x8');
      writer.putLdrRegAddress('x16', callback); writer.putBlrReg('x16');
      writer.putTbnzRegImmLabel('w0', 31, 'fatal'); writer.putCmpRegReg('w0', 'wzr');
      writer.putPopRegReg('x8', 'lr'); writer.putPopRegReg('x0', 'x1');
      writer.putBCondLabel('eq', 'fallback'); writer.putRet();
      writer.putLabel('fallback'); writer.putLdrRegAddress('x16', slot); writer.putLdrRegRegOffset('x16', 'x16', 0); writer.putBrReg('x16');
      writer.putLabel('fatal'); writer.putLdrRegAddress('x16', Process.getModuleByName('libc.so').getExportByName('abort')); writer.putBrReg('x16');
      writer.flush();
    });
    retained.push(callback, slot, code);
    const target = engine.base.add(writing ? config.write_bridge : config.read_bridge);
    slot.writePointer(Interceptor.replaceFast(target, code));
    return target;
  };
  const targets = [];
  try { targets.push(make(false)); targets.push(make(true)); Interceptor.flush(); installed = true; }
  catch (failure) { for (const target of targets.reverse()) Interceptor.revert(target); throw failure; }
  return {
    phase(value) { phase = value; },
    retire() {
      if (!installed || retired) throw new Error('PN bridge lifecycle differs');
      for (const target of targets) Interceptor.revert(target);
      Interceptor.flush();
      installed = false; retired = true;
      if (nativeBackend) nativeBackend.retire();
      return true;
    },
    snapshot() {
      return {schema: nativeBackend ? (config.codec_backend.mode === 'persistent-pn-edges-v1'
        ? 'e2.1m-cold-pn-persistent-bridge.v1' : 'e2.1k-cold-pn-codec-bridge.v1') :
        (coldInstall ? 'e2.1i-cold-pn-bridge.v1' : 'e2.1h-pn-bridge.v1'), installed, retired, events, errors, fallbacks,
        retained_allocations: retained.length,
        native_backend: nativeBackend ? nativeBackend.snapshot() : null,
        scope: nativeBackend ? 'first-native-bridge PN control and native codec; original store constructors/XML/backup and JavaVM retained'
          : (coldInstall ? 'first-native-bridge PN control; original per-store Cipher/XML/backup and JavaVM retained'
            : 'post-cold-ready PN bridge; original per-store Cipher/XML/backup and JavaVM retained'),
        original_xml_writer_replaced: false, backup_delivery: 'unknown', concurrency_equivalence: 'unknown',
        no_vm_verified: false, strict_gate: 'BLOCKED'};
    }
  };
}
