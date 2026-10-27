'use strict';

function createPNNativeBackend(config, invoke, library) {
  if (config.diagnostics !== undefined && config.diagnostics !== 'call-timeline-v1') throw new Error('unknown PN diagnostics');
  const diagnostics = config.diagnostics === 'call-timeline-v1';
  const timeline = [];
  let timelineDropped = 0, callSequence = 0, owner = null;
  const observe = (edge, detail) => {
    if (!diagnostics) return;
    if (timeline.length >= 4096) { timelineDropped++; return; }
    const row = Object.assign({sequence: timeline.length + 1, edge, thread: Process.getCurrentThreadId(),
      wall_milliseconds: Date.now(), owner: owner ? Object.assign({}, owner) : null}, detail);
    timeline.push(row);
    send({event: 'pn_backend_timeline', detail: row});
  };
  const step = (stage, callback) => {
    if (!diagnostics) return callback();
    const previous = owner ? owner.stage : null;
    if (owner) owner.stage = stage;
    observe('stage-enter', {stage});
    try {
      const result = callback();
      observe('stage-return', {stage});
      return result;
    } catch (failure) {
      observe('stage-throw', {stage, error: String(failure)});
      throw failure;
    } finally { if (owner) owner.stage = previous; }
  };
  const rawInvoke = operation => step('jni:' + operation.split(':', 1)[0], () => invoke(operation));
  const persistent = config.mode === 'persistent-pn-edges-v1';
  if (Process.arch !== 'arm64' || !library || (!persistent && config.mode !== 'present-ciphertext-v1') ||
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
  const stream = persistent ? {
    open: new NativeFunction(library.getExportByName('e21_stream_open'), 'int',
      ['pointer', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer']),
    decode: new NativeFunction(library.getExportByName('e21_stream_decode'), 'int',
      ['pointer', 'pointer', 'ulong', 'pointer', 'ulong', 'pointer']),
    close: new NativeFunction(library.getExportByName('e21_stream_close'), 'void', ['pointer'])
  } : null;
  const decoders = [];
  const keys = Memory.alloc(64), iv = Memory.alloc(16);
  keys.writeByteArray(new Array(64).fill(0));
  iv.writeByteArray(new Array(16).fill(0));
  const events = [], errors = [];
  let context = null, started = false, ready = false, bound = false, retired = false, busy = false;
  let derivations = 0, transforms = 0;
  const decoderCalls = [0, 0];
  const decoderFailures = [0, 0];
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
    const status = step('native:transform', () => transform(context, mode, encrypt, keys.add(store * 32), 32,
      mode ? iv : ptr(0), mode ? 16 : 0, input, bytes.length, output, 4128, length));
    transforms++;
    const amount = length.readU64().toNumber();
    if (status !== 0 || amount > 4112) throw new Error('native PN codec failed: ' + status);
    return Array.from(new Uint8Array(output.readByteArray(amount)));
  };
  const remove = store => {
    const result = JSON.parse(rawInvoke('native-remove:' + store));
    if (result.commit !== true) throw new Error('original XML remove commit returned false; partial effects retained');
  };
  const decrypt = (store, bytes) => {
    const input = Memory.alloc(Math.max(1, bytes.length));
    if (bytes.length) input.writeByteArray(bytes);
    const output = Memory.alloc(4128), length = Memory.alloc(8);
    length.writeU64(0);
    const status = step('native:stream-decode', () => stream.decode(decoders[store], input, bytes.length, output, 4128, length));
    transforms++;
    decoderCalls[store]++;
    const amount = length.readU64().toNumber();
    if (amount > 4112 || (status !== 0 && amount !== 0)) throw new Error('persistent decoder output contract differs');
    if (status === -4) {
      decoderFailures[store]++;
      return null;
    }
    if (status !== 0) throw new Error('persistent decoder failed outside observed crypto contract: ' + status);
    return Array.from(new Uint8Array(output.readByteArray(amount)));
  };
  const initialize = () => {
    if (ready) return;
    if (started || retired) throw new Error('PN native initialization already failed or retired');
    started = true;
    const source = JSON.parse(rawInvoke('derivation-source'));
    if (source.source !== 'original-app-context Settings.Secure' || source.package !== config.package ||
        typeof source.identity !== 'string' || !/^[0-9a-f]{16}$/.test(source.identity)) {
      throw new Error('PN source identity outside explicit contract');
    }
    const table = Memory.alloc(96), material = Memory.alloc(config.iv_source.length);
    config.table.forEach((value, index) => table.add(index * 4).writeS32(value));
    material.writeByteArray(config.iv_source);
    const status = step('native:derive', () => derive(Memory.allocUtf8String(config.crypto_library), Memory.allocUtf8String(source.identity), 16,
      Memory.allocUtf8String(source.package), source.package.length, table, 24, material, config.iv_source.length,
      keys, 64, iv, 16));
    derivations++;
    if (status !== 0) throw new Error('native PN derivation failed: ' + status);
    const output = Memory.alloc(Process.pointerSize);
    output.writePointer(ptr(0));
    const opened = step('native:open', () => open(Memory.allocUtf8String(config.crypto_library), output));
    if (opened !== 0 || output.readPointer().isNull()) throw new Error('native PN codec open failed: ' + opened);
    context = output.readPointer();
    if (persistent) {
      for (let store = 0; store < 2; store++) {
        const destination = Memory.alloc(Process.pointerSize);
        destination.writePointer(ptr(0));
        const opened = step('native:stream-open', () => stream.open(Memory.allocUtf8String(config.crypto_library), keys.add(store * 32), 32, iv, 16, destination));
        if (opened !== 0 || destination.readPointer().isNull()) throw new Error('persistent decoder open failed: ' + opened);
        decoders.push(destination.readPointer());
      }
    }
    const encodedKeys = [0, 1].map(store => encode(crypt(store, 0, 1, [80, 78, 95, 80, 79, 80, 85, 80])));
    if (rawInvoke('native-bind:' + encodedKeys.join(':')) !== 'native-bound\t2') throw new Error('PN raw bind receipt differs');
    bound = true;
    ready = true;
    events.push({operation: 'initialize', thread: Process.getCurrentThreadId(), status: 'ready',
      identity_source: source.source, original_store_constructors: 'retained'});
  };
  const call = (operation, store, callback) => {
    if (retired || busy || ![0, 1].includes(store) || events.length >= 1024) {
      observe('rejected', {operation, store, busy, retired, reason: 'bounded serial PN backend required'});
      throw new Error('bounded serial PN backend required');
    }
    busy = true;
    if (diagnostics) owner = {call: ++callSequence, operation, store, thread: Process.getCurrentThreadId(), stage: 'entered'};
    observe('call-enter', {operation, store});
    const row = {operation, store, thread: Process.getCurrentThreadId(), status: 'entered'};
    try {
      initialize();
      events.push(row);
      const result = callback();
      row.status = 'returned';
      observe('call-return', {operation, store});
      return result;
    } catch (failure) {
      row.status = 'failed';
      errors.push(String(failure));
      observe('call-throw', {operation, store, error: String(failure)});
      throw failure;
    } finally { busy = false; owner = null; }
  };
  return {
    read(store) {
      return call('read', store, () => {
        const raw = JSON.parse(rawInvoke('native-get:' + store));
        if (typeof raw.present !== 'boolean' || typeof raw.plain_present !== 'boolean') {
          throw new Error('raw PN entry shape differs');
        }
        if (persistent && !raw.present) return [];
        if (!persistent && (raw.present !== true || raw.plain_present !== false)) {
          throw new Error('missing or legacy PN entry outside present-ciphertext contract');
        }
        const input = persistent && raw.encoded === '%%%' ? [] : decode(raw.encoded);
        const bytes = persistent ? decrypt(store, input) : crypt(store, 1, 0, input);
        if (persistent && (bytes === null || bytes.length === 0)) {
          remove(store);
          return [];
        }
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
        if (persistent && !bytes.length) {
          remove(store);
          return;
        }
        if (!bytes.length || bytes.length > 4095 || bytes.includes(0)) throw new Error('empty or binary PN write outside contract');
        const encoded = encode(crypt(store, 1, 1, bytes));
        const result = JSON.parse(rawInvoke('native-put:' + store + ':' + encoded));
        if (result.commit !== true) throw new Error('original XML commit returned false; partial effects retained');
      });
    },
    retire() {
      if (retired || busy) throw new Error('PN native lifecycle differs');
      try {
        if (bound && rawInvoke('native-unbind') !== 'native-unbound\t2') throw new Error('PN raw unbind receipt differs');
        bound = false;
      } finally {
        if (stream) decoders.forEach(decoder => stream.close(decoder));
        decoders.length = 0;
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
      return {schema: persistent ? 'e2.1m-natural-pn-persistent-codec.v1' : 'e2.1k-natural-pn-native-codec.v1',
        started, ready, retired, derivations, transforms, events, errors,
        mode: config.mode, decoder_calls: decoderCalls.slice(), decoder_failures: decoderFailures.slice(),
        ...(diagnostics ? {diagnostics: {schema: 'e2.1n-backend-timeline.v1', events: timeline, dropped: timelineDropped,
          owner: owner ? Object.assign({}, owner) : null, busy, general_thread_safety: 'unknown'}} : {}),
        xml_backend: 'original SharedPreferences', original_store_constructors: 'retained',
        unsupported: persistent ? ['unobserved-base64', 'binary-or-invalid-utf8', 'commit-false-equivalence', 'concurrency']
          : ['missing', 'legacy-plain', 'empty', 'malformed', 'commit-false-equivalence', 'concurrency'],
        no_vm_verified: false, strict_gate: 'BLOCKED'};
    }
  };
}
