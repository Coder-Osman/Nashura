'use strict';

let settings = null;
let engine = null;
let construct = null;
const retained = [];
const results = [];

function decode(pointer) {
  const length = pointer.add(4).readU32();
  if (length > 4095) throw new Error('output exceeds bounded EngineString16 probe');
  const data = length < 8 ? pointer.add(8) : pointer.add(8).readPointer().and('0x00ffffffffffffff');
  if (data.isNull() || data.add(length).readU8() !== 0) throw new Error('invalid output string');
  return {length, value: length ? data.readUtf8String(length) : '',
    bytes: Array.from(new Uint8Array(data.readByteArray(length)))};
}

function string(value) {
  if (value.includes('\0') || unescape(encodeURIComponent(value)).length > 4095) throw new Error('input outside probe contract');
  const output = Memory.alloc(16);
  const input = Memory.allocUtf8String(value);
  retained.push(output, input);
  construct(output, input);
  return output;
}

function readFunction(offset) {
  const code = Memory.alloc(Process.pageSize);
  Memory.patchCode(code, Process.pageSize, region => {
    const writer = new Arm64Writer(region, {pc: code});
    writer.putMovRegReg('x8', 'x1');
    writer.putLdrRegAddress('x16', engine.base.add(offset));
    writer.putBrReg('x16');
    writer.flush();
  });
  retained.push(code);
  return new NativeFunction(code, 'void', ['pointer', 'pointer']);
}

rpc.exports = {
  configure(config) {
    if (settings) throw new Error('immutable probe configuration');
    if (Process.arch !== 'arm64') throw new Error('probe requires verified AArch64 ABI');
    settings = config;
    engine = Process.getModuleByName(config.engine_module);
    for (const offset of [config.string_constructor, config.read_bridge, config.write_bridge]) {
      const range = Process.findRangeByAddress(engine.base.add(offset));
      if (!range || !range.protection.includes('x')) throw new Error('nonexecutable probe target');
    }
    construct = new NativeFunction(engine.base.add(config.string_constructor), 'void', ['pointer', 'pointer']);
    return true;
  },
  batch(cases) {
    if (!settings || cases.length > 64 || results.length + cases.length > 256) throw new Error('bounded probe budget exceeded');
    const read = readFunction(settings.read_bridge);
    const write = new NativeFunction(engine.base.add(settings.write_bridge), 'void', ['pointer', 'pointer']);
    let exception = null;
    if (settings.jni_attach !== undefined) {
      const attach = new NativeFunction(engine.base.add(settings.jni_attach), 'pointer', []);
      const environment = attach();
      if (environment.isNull() || settings.jni_exception_check_slot !== 228) throw new Error('JNIEnv contract mismatch');
      const check = new NativeFunction(environment.readPointer().add(228 * Process.pointerSize).readPointer(), 'uchar', ['pointer']);
      exception = () => Boolean(check(environment));
    }
    const rows = [];
    for (const item of cases) {
      if (!['read', 'write', 'exists', 'delete'].includes(item.operation)) throw new Error('undeclared operation');
      const entry = item.entry || item.operation + '_bridge';
      if (!['read_bridge', 'write_bridge', 'preference_read', 'preference_write', 'exists_bridge', 'delete_bridge'].includes(entry) ||
          !Number.isInteger(settings[entry])) throw new Error('undeclared native entry');
      if ((item.operation === 'read' && !['read_bridge', 'preference_read'].includes(entry)) ||
          (item.operation === 'write' && !['write_bridge', 'preference_write'].includes(entry)) ||
          (item.operation === 'exists' && entry !== 'exists_bridge') ||
          (item.operation === 'delete' && entry !== 'delete_bridge')) throw new Error('operation/entry mismatch');
      const row = {case: item.case, operation: item.operation, key: item.key,
        thread: Process.getCurrentThreadId(), entry_offset: settings[entry], entry,
        error_status: 'unknown; original bridge exposes no typed error',
        exception_state: 'unknown; no pending-exception query injected'};
      results.push(row);
      send({event: 'e2_1_probe_begin', detail: {case: item.case, operation: item.operation, key: item.key, entry}});
      const before = exception ? exception() : null;
      if (before) throw new Error('preexisting JNI exception; probe stops without clearing');
      if (item.operation === 'write') {
        const source = item.restore_from_case ? results.find(previous => previous.case === item.restore_from_case && previous.result) : null;
        if (item.restore_from_case && !source) throw new Error('restore source missing');
        const value = source ? source.result.value : item.value;
        const target = entry === 'write_bridge' ? write : new NativeFunction(engine.base.add(settings[entry]), 'void', ['pointer', 'pointer']);
        target(string(item.key), string(value));
        row.input = value;
        row.return_type = 'void; raw register not interpreted as status';
      } else if (item.operation === 'read') {
        const output = Memory.alloc(16);
        retained.push(output);
        const target = entry === 'read_bridge' ? read : readFunction(settings[entry]);
        target(string(item.key), output);
        row.result = decode(output);
        row.return_type = 'EngineString16 through AArch64 x8';
      } else {
        const target = new NativeFunction(engine.base.add(settings[entry]), item.operation === 'exists' ? 'uchar' : 'void', ['pointer']);
        const result = target(string(item.key));
        row.return_type = item.operation === 'exists' ? 'bool; checked against source wrapper' : 'void; no success code inferred';
        if (item.operation === 'exists') row.result = {boolean: Boolean(result)};
      }
      if (exception) row.exception_state = {before, after: exception(), source: 'explicit JNIEnv.ExceptionCheck on probe thread'};
      rows.push(row);
      send({event: 'e2_1_probe_result', detail: row});
      if (row.exception_state.after) throw new Error('JNI exception left pending; probe stops without clearing');
    }
    return rows;
  },
  snapshot() {
    return {schema: 'e2.1-native-bridge-probe.v1', results,
      ownership: 'bounded process-lifetime retention; engine string destructor not verified',
      close_free: 'unknown', repeated_configuration_constructor: 'not called; object ownership unknown',
      no_vm_verified: false};
  }
};
