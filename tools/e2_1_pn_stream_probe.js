'use strict';

let api = null, keys = null, iv = null;
const contexts = [], calls = [0, 0];
let closed = false;

rpc.exports = {
  configure(config, source) {
    if (api || Process.arch !== 'arm64' || typeof config.library !== 'string' || !config.library.startsWith('/') ||
        typeof config.crypto_library !== 'string' || !config.crypto_library.startsWith('/') ||
        !Array.isArray(config.table) || config.table.length !== 24 ||
        config.table.some(value => !Number.isInteger(value) || value < -2147483648 || value > 2147483647) ||
        !Array.isArray(config.iv_source) || config.iv_source.length < 16 || config.iv_source.length > 4095 ||
        config.iv_source.some(value => !Number.isInteger(value) || value < 0 || value > 255) ||
        source.source !== 'original-app-context Settings.Secure' || source.package !== config.package ||
        typeof source.identity !== 'string' || !/^[0-9a-f]{16}$/.test(source.identity)) {
      throw new Error('bounded explicit stream derivation contract required');
    }
    const library = Module.load(config.library);
    api = {
      open: new NativeFunction(library.getExportByName('e21_stream_open'), 'int',
        ['pointer', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer']),
      decode: new NativeFunction(library.getExportByName('e21_stream_decode'), 'int',
        ['pointer', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer']),
      close: new NativeFunction(library.getExportByName('e21_stream_close'), 'void', ['pointer'])
    };
    keys = Memory.alloc(64); iv = Memory.alloc(16);
    const table = Memory.alloc(96), material = Memory.alloc(config.iv_source.length);
    config.table.forEach((value, index) => table.add(index * 4).writeS32(value));
    material.writeByteArray(config.iv_source);
    const derive = new NativeFunction(library.getExportByName('e21_pn_derive'), 'int',
      ['pointer', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong']);
    try {
      const status = derive(Memory.allocUtf8String(config.crypto_library), Memory.allocUtf8String(source.identity), 16,
        Memory.allocUtf8String(config.package), config.package.length, table, 24, material, config.iv_source.length,
        keys, 64, iv, 16);
      if (status !== 0) throw new Error('stream derivation failed: ' + status);
      for (let store = 0; store < 2; store++) {
        const output = Memory.alloc(Process.pointerSize); output.writePointer(ptr(0));
        const opened = api.open(Memory.allocUtf8String(config.crypto_library), keys.add(store * 32), 32, iv, 16, output);
        if (opened !== 0 || output.readPointer().isNull()) throw new Error('stream open failed: ' + opened);
        contexts.push(output.readPointer());
      }
    } catch (failure) {
      contexts.forEach(context => api.close(context));
      keys.writeByteArray(new Array(64).fill(0)); iv.writeByteArray(new Array(16).fill(0));
      closed = true;
      throw failure;
    }
    return {schema: 'e2.1l-persistent-decoder.v1', stores: 2,
      reset: 'real EVP context reinitialized only after successful final',
      error: 'real EVP state retained on failed final', bridge_replaced: false, no_vm_verified: false};
  },
  decode(store, bytes) {
    if (!api || closed || contexts.length !== 2 || ![0, 1].includes(store) || calls[store] >= 16 ||
        !Array.isArray(bytes) || bytes.length > 4096 ||
        bytes.some(value => !Number.isInteger(value) || value < 0 || value > 255)) {
      throw new Error('bounded live serial decoder required');
    }
    const input = Memory.alloc(Math.max(1, bytes.length));
    if (bytes.length) input.writeByteArray(bytes);
    const output = Memory.alloc(4128), length = Memory.alloc(8); length.writeU64(0);
    const status = api.decode(contexts[store], input, bytes.length, output, 4128, length);
    calls[store]++;
    const amount = length.readU64().toNumber();
    if (amount > 4112 || (status !== 0 && amount !== 0)) throw new Error('stream output contract differs');
    return {status, output: Array.from(new Uint8Array(output.readByteArray(amount)))};
  },
  snapshot() {
    return {schema: 'e2.1l-persistent-decoder-state.v1', stores: contexts.length,
      calls: calls.slice(), closed, bridge_replaced: false, no_vm_verified: false};
  },
  close() {
    if (!api || closed) throw new Error('live stream required');
    contexts.forEach(context => api.close(context));
    keys.writeByteArray(new Array(64).fill(0)); iv.writeByteArray(new Array(16).fill(0));
    closed = true;
    return {closed, stores: contexts.length, calls, bridge_replaced: false, no_vm_verified: false};
  }
};
