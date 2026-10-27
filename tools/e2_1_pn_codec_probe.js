'use strict';

let settings = null;
let api = null;
let context = null;
let calls = 0;
let closed = false;

function bytes(value, limit) {
  if (!Array.isArray(value) || value.length > limit || value.some(item => !Number.isInteger(item) || item < 0 || item > 255)) {
    throw new Error('bounded explicit byte vector required');
  }
  const result = Memory.alloc(Math.max(1, value.length));
  if (value.length) result.writeByteArray(value);
  return result;
}

rpc.exports = {
  configure(config) {
    if (settings || Process.arch !== 'arm64' || typeof config.library !== 'string' ||
        typeof config.crypto_library !== 'string' || !config.library.startsWith('/') ||
        !config.crypto_library.startsWith('/')) throw new Error('immutable explicit codec libraries required');
    const module = Module.load(config.library);
    api = {
      open: new NativeFunction(module.getExportByName('e21_codec_open'), 'int', ['pointer', 'pointer']),
      transform: new NativeFunction(module.getExportByName('e21_codec_transform'), 'int',
        ['pointer', 'int', 'int', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer']),
      close: new NativeFunction(module.getExportByName('e21_codec_close'), 'void', ['pointer'])
    };
    const output = Memory.alloc(Process.pointerSize);
    output.writePointer(ptr(0));
    const status = api.open(Memory.allocUtf8String(config.crypto_library), output);
    if (status !== 0 || output.readPointer().isNull()) throw new Error('explicit crypto provider open failed: ' + status);
    context = output.readPointer();
    settings = config;
    return {provider_path: Process.getModuleByName(config.crypto_library.split('/').pop()).path,
      native_codec_path: module.path, scope: 'explicit-byte codec; no PN bridge or XML writer installed'};
  },
  transform(item) {
    if (!settings || closed || ++calls > 256 || ![0, 1].includes(item.mode) || ![0, 1].includes(item.encrypt)) {
      throw new Error('bounded live codec call required');
    }
    const key = bytes(item.key, 32);
    const iv = bytes(item.iv, 16);
    const input = bytes(item.input, 4096);
    const output = Memory.alloc(4128);
    const length = Memory.alloc(8);
    length.writeU64(0);
    const status = api.transform(context, item.mode, item.encrypt, key, item.key.length, iv,
      item.iv.length, input, item.input.length, output, 4128, length);
    const amount = length.readU64().toNumber();
    if (amount > 4112 || (status !== 0 && amount !== 0)) throw new Error('codec output contract violation');
    return {status, output: Array.from(new Uint8Array(output.readByteArray(amount)))};
  },
  close() {
    if (!settings || closed) throw new Error('live codec required');
    api.close(context);
    context = null;
    closed = true;
    return {closed, calls};
  },
  snapshot() {
    return {schema: 'e2.1-pn-native-codec.v1', calls, closed,
      bridge_replacement: false, original_xml_writer: false, no_vm_verified: false};
  }
};
