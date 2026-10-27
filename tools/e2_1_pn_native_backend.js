'use strict';

function createPNNativeBackend(config, invoke, library) {
  if (Process.arch !== 'arm64' || !library || config.mode !== 'present-ciphertext-v1' ||
      typeof config.crypto_library !== 'string' || !config.crypto_library.startsWith('/') ||
      !Array.isArray(config.table) || config.table.length !== 24 ||
      config.table.some(value => !Number.isInteger(value) || value < -2147483648 || value > 2147483647) ||
      !Array.isArray(config.iv_source) || config.iv_source.length < 16 || config.iv_source.length > 4095 ||
      config.iv_source.some(value => !Number.isInteger(value) || value < 0 || value > 255) ||
      typeof config.package !== 'string' || !/^[A-Za-z0-9._]{1,255}$/.test(config.package)) {
    throw new Error('explicit bounded PN native codec contract required');
  }
  const derive = new NativeFunction(library.getExportByName('e21_pn_derive'), 'int',
    ['pointer', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong',
      'pointer', 'ulong', 'pointer', 'ulong']);
  const open = new NativeFunction(library.getExportByName('e21_codec_open'), 'int', ['pointer', 'pointer']);
  const transform = new NativeFunction(library.getExportByName('e21_codec_transform'), 'int',
    ['pointer', 'int', 'int', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer']);
  const close = new NativeFunction(library.getExportByName('e21_codec_close'), 'void', ['pointer']);
  const keys = Memory.alloc(64), iv = Memory.alloc(16);
  keys.writeByteArray(new Array(64).fill(0));
  iv.writeByteArray(new Array(16).fill(0));
  const events = [], errors = [];
  let context = null, started = false, ready = false, bound = false, retired = false, busy = false;
  let derivations = 0, transforms = 0;
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const encode = bytes => {
    let result = '';
    for (let index = 0; index < bytes.length; index += 3) {
      const bits = (bytes[index] << 16) | ((bytes[index + 1] || 0) << 8) | (bytes[index + 2] || 0);
      result += alphabet[(bits >>> 18) & 63] + alphabet[(bits >>> 12) & 63] +
        (index + 1 < bytes.length ? alphabet[(bits >>> 6) & 63] : '=') +
        (index + 2 < bytes.length ? alphabet[bits & 63] : '=');
    }
    return result;
  };
  const decode = value => {
    if (typeof value !== 'string' || value.length > 5464 || value.length % 4 ||
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
      throw new Error('uncovered ciphertext encoding; original raw entry preserved');
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
    if (encode(bytes) !== value) throw new Error('noncanonical ciphertext; raw entry preserved');
    return bytes;
  };
  const crypt = (store, mode, encrypt, bytes) => {
    const input = Memory.alloc(Math.max(1, bytes.length));
    if (bytes.length) input.writeByteArray(bytes);
    const output = Memory.alloc(4128), length = Memory.alloc(8);
    length.writeU64(0);
    const status = transform(context, mode, encrypt, keys.add(store * 32), 32,
      mode ? iv : ptr(0), mode ? 16 : 0, input, bytes.length, output, 4128, length);
    transforms++;
    const amount = length.readU64().toNumber();
    if (status !== 0 || amount > 4112) throw new Error('native PN codec failed: ' + status);
    return Array.from(new Uint8Array(output.readByteArray(amount)));
  };
  const initialize = () => {
    if (ready) return;
    if (started || retired) throw new Error('PN native initialization already failed or retired');
    started = true;
    const source = JSON.parse(invoke('derivation-source'));
    if (source.source !== 'original-app-context Settings.Secure' || source.package !== config.package ||
        typeof source.identity !== 'string' || !/^[0-9a-f]{16}$/.test(source.identity)) {
      throw new Error('PN source identity outside explicit contract');
    }
    const table = Memory.alloc(96), material = Memory.alloc(config.iv_source.length);
    config.table.forEach((value, index) => table.add(index * 4).writeS32(value));
    material.writeByteArray(config.iv_source);
    const status = derive(Memory.allocUtf8String(config.crypto_library), Memory.allocUtf8String(source.identity), 16,
      Memory.allocUtf8String(source.package), source.package.length, table, 24, material, config.iv_source.length,
      keys, 64, iv, 16);
    derivations++;
    if (status !== 0) throw new Error('native PN derivation failed: ' + status);
    const output = Memory.alloc(Process.pointerSize);
    output.writePointer(ptr(0));
    const opened = open(Memory.allocUtf8String(config.crypto_library), output);
    if (opened !== 0 || output.readPointer().isNull()) throw new Error('native PN codec open failed: ' + opened);
    context = output.readPointer();
    const encodedKeys = [0, 1].map(store => encode(crypt(store, 0, 1, [80, 78, 95, 80, 79, 80, 85, 80])));
    if (invoke('native-bind:' + encodedKeys.join(':')) !== 'native-bound\t2') throw new Error('PN raw bind receipt differs');
    bound = true;
    ready = true;
    events.push({operation: 'initialize', thread: Process.getCurrentThreadId(), status: 'ready',
      identity_source: source.source, original_store_constructors: 'retained'});
  };
  const call = (operation, store, callback) => {
    if (retired || busy || ![0, 1].includes(store) || events.length >= 1024) throw new Error('bounded serial PN backend required');
    busy = true;
    const row = {operation, store, thread: Process.getCurrentThreadId(), status: 'entered'};
    try {
      initialize();
      events.push(row);
      const result = callback();
      row.status = 'returned';
      return result;
    } catch (failure) {
      row.status = 'failed';
      errors.push(String(failure));
      throw failure;
    } finally { busy = false; }
  };
  return {
    read(store) {
      return call('read', store, () => {
        const raw = JSON.parse(invoke('native-get:' + store));
        if (raw.present !== true || raw.plain_present !== false) {
          throw new Error('missing or legacy PN entry outside present-ciphertext contract');
        }
        const bytes = crypt(store, 1, 0, decode(raw.encoded));
        if (!bytes.length || bytes.length > 4095 || bytes.includes(0)) throw new Error('empty or binary PN outside contract; raw entry preserved');
        const text = Memory.alloc(bytes.length + 1);
        text.writeByteArray(bytes.concat([0]));
        const value = text.readUtf8String(bytes.length);
        if (unescape(encodeURIComponent(value)) !== String.fromCharCode(...bytes)) throw new Error('invalid PN UTF-8; raw entry preserved');
        return bytes;
      });
    },
    write(store, text) {
      return call('write', store, () => {
        const bytes = Array.from(unescape(encodeURIComponent(text)), value => value.charCodeAt(0));
        if (!bytes.length || bytes.length > 4095 || bytes.includes(0)) throw new Error('empty or binary PN write outside contract');
        const encoded = encode(crypt(store, 1, 1, bytes));
        const result = JSON.parse(invoke('native-put:' + store + ':' + encoded));
        if (result.commit !== true) throw new Error('original XML commit returned false; partial effects retained');
      });
    },
    retire() {
      if (retired || busy) throw new Error('PN native lifecycle differs');
      try {
        if (bound && invoke('native-unbind') !== 'native-unbound\t2') throw new Error('PN raw unbind receipt differs');
        bound = false;
      } finally {
        if (context) close(context);
        context = null;
        keys.writeByteArray(new Array(64).fill(0));
        iv.writeByteArray(new Array(16).fill(0));
        ready = false;
        retired = true;
      }
      return true;
    },
    snapshot() {
      return {schema: 'e2.1k-natural-pn-native-codec.v1', started, ready, retired, derivations, transforms, events, errors,
        xml_backend: 'original SharedPreferences', original_store_constructors: 'retained',
        unsupported: ['missing', 'legacy-plain', 'empty', 'malformed', 'commit-false-equivalence', 'concurrency'],
        no_vm_verified: false, strict_gate: 'BLOCKED'};
    }
  };
}
