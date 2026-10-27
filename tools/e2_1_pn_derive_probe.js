'use strict';

let settings = null;
let derive = null;
let calls = 0;

function bytes(value, maximum) {
  if (!Array.isArray(value) || value.length > maximum || value.some(item => !Number.isInteger(item) || item < 0 || item > 255)) {
    throw new Error('bounded byte vector required');
  }
  const result = Memory.alloc(Math.max(value.length, 1));
  if (value.length) result.writeByteArray(value);
  return result;
}

rpc.exports = {
  configure(config) {
    if (settings || Process.arch !== 'arm64' || typeof config.library !== 'string' ||
        !config.library.startsWith('/') || typeof config.crypto_library !== 'string' ||
        !config.crypto_library.startsWith('/')) throw new Error('explicit immutable native libraries required');
    const module = Module.load(config.library);
    derive = new NativeFunction(module.getExportByName('e21_pn_derive'), 'int',
      ['pointer', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong',
       'pointer', 'ulong', 'pointer', 'ulong']);
    settings = config;
    return {library: module.path, scope: 'post-trajectory explicit-source derivation; original initialization retained'};
  },
  run(item) {
    if (!settings || ++calls > 16 || !Array.isArray(item.table) || item.table.length > 24 ||
        item.table.some(value => !Number.isInteger(value) || value < -2147483648 || value > 2147483647)) {
      throw new Error('bounded explicit source contract required');
    }
    const table = Memory.alloc(96);
    item.table.forEach((value, index) => table.add(index * 4).writeS32(value));
    const keys = Memory.alloc(64), iv = Memory.alloc(16);
    keys.writeByteArray(new Array(64).fill(165));
    iv.writeByteArray(new Array(16).fill(165));
    const status = derive(Memory.allocUtf8String(settings.crypto_library), bytes(item.identity, 32), item.identity.length,
      bytes(item.package, 255), item.package.length, table, item.table.length,
      bytes(item.iv_source, 4095), item.iv_source.length, keys, 64, iv, 16);
    return {status, keys: Array.from(new Uint8Array(keys.readByteArray(64))),
      iv: Array.from(new Uint8Array(iv.readByteArray(16)))};
  },
  snapshot() {
    return {schema: 'e2.1j-native-derivation.v1', calls, initialization_replaced: false,
      original_xml_writer_replaced: false, no_vm_verified: false};
  }
};
