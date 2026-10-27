'use strict';
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Pointer {
  constructor(buffer, offset = 0) { this.buffer = buffer; this.offset = offset; }
  add(amount) { return new Pointer(this.buffer, this.offset + amount); }
  writeByteArray(bytes) { Buffer.from(bytes).copy(this.buffer, this.offset); }
  readByteArray(amount) { return Uint8Array.from(this.buffer.subarray(this.offset, this.offset + amount)).buffer; }
  writeU64(value) { this.buffer.writeBigUInt64LE(BigInt(value), this.offset); }
  readU64() { return {toNumber: () => Number(this.buffer.readBigUInt64LE(this.offset))}; }
  writeS32(value) { this.buffer.writeInt32LE(value, this.offset); }
  writePointer(value) { this.pointer = value; }
  readPointer() { return this.pointer; }
  readUtf8String(amount) {
    const bytes = this.buffer.subarray(this.offset, amount === undefined ? this.buffer.indexOf(0, this.offset) : this.offset + amount);
    return new TextDecoder('utf-8', {fatal: true}).decode(bytes);
  }
  isNull() { return false; }
}

function fixture(failure, mode = 'present-ciphertext-v1', options = {}) {
  const operations = [], buffers = [];
  const messages = [];
  let thread = 11;
  let closes = 0;
  let streamOpens = 0, streamCloses = 0;
  const streamCalls = [0, 0], plain = [false, false];
  const keys = [Buffer.alloc(32, 1), Buffer.alloc(32, 2)], iv = Buffer.alloc(16, 3);
  const encrypt = (store, mode, text) => {
    const cipher = crypto.createCipheriv(mode ? 'aes-256-cbc' : 'aes-256-ecb', keys[store], mode ? iv : null);
    return Buffer.concat([cipher.update(text), cipher.final()]).toString('base64');
  };
  const raw = [encrypt(0, 1, 'earlier'), encrypt(1, 1, 'preferred')];
  const functions = {
    e21_stream_open(library, key, keyLength, vector, vectorLength, output) {
      const store = streamOpens++;
      if (failure === 'stream-open-' + store) return -2;
      const handle = new Pointer(Buffer.alloc(1)); handle.store = store;
      output.writePointer(handle); return 0;
    },
    e21_stream_close() { streamCloses++; },
    e21_stream_decode(handle, input, inputLength, output, capacity, length) {
      streamCalls[handle.store]++;
      if (failure === 'stream-status') return -3;
      if (failure === 'stream-partial') { length.writeU64(1); return -4; }
      return functions.e21_codec_transform(handle, 1, 0, new Pointer(keys[handle.store]), 32,
        new Pointer(iv), 16, input, inputLength, output, capacity, length);
    },
    e21_pn_derive(library, identity, identityLength, packageName, packageLength, table, tableCount, material,
      materialLength, outputKeys, capacity, outputIv) {
      assert.equal(identity.readUtf8String(), '0123456789abcdef');
      assert.equal(identityLength, 16); assert.equal(tableCount, 24);
      if (failure === 'derive') return -1;
      outputKeys.writeByteArray(Buffer.concat(keys)); outputIv.writeByteArray(iv); return 0;
    },
    e21_codec_open(library, output) { if (failure === 'open') return -2; output.writePointer(new Pointer(Buffer.alloc(1))); return 0; },
    e21_codec_close() { closes++; },
    e21_codec_transform(handle, mode, encrypting, key, keyLength, vector, vectorLength, input, inputLength, output, capacity, length) {
      assert.equal(keyLength, 32); assert.equal(vectorLength, mode ? 16 : 0);
      const aesKey = Buffer.from(key.readByteArray(32)), aesIv = mode ? Buffer.from(vector.readByteArray(16)) : null;
      length.writeU64(0);
      try {
        const cipher = encrypting ? crypto.createCipheriv(mode ? 'aes-256-cbc' : 'aes-256-ecb', aesKey, aesIv)
          : crypto.createDecipheriv(mode ? 'aes-256-cbc' : 'aes-256-ecb', aesKey, aesIv);
        const result = Buffer.concat([cipher.update(Buffer.from(input.readByteArray(inputLength))), cipher.final()]);
        output.writeByteArray(result); length.writeU64(result.length); return 0;
      } catch { return -4; }
    }
  };
  const context = {
    Process: {arch: 'arm64', pointerSize: 8, getCurrentThreadId: () => thread},
    send(message) { messages.push(message); },
    Memory: {alloc(size) { const pointer = new Pointer(Buffer.alloc(size)); buffers.push(pointer); return pointer; },
      allocUtf8String(text) { return new Pointer(Buffer.from(text + '\0')); }},
    NativeFunction: function(address) { return functions[address]; },
    ptr: () => ({isNull: () => true}), Uint8Array, unescape, encodeURIComponent
  };
  const invoke = operation => {
    operations.push(operation.split(':').slice(0, 2).join(':'));
    if (operation === 'derivation-source') return JSON.stringify({source: 'original-app-context Settings.Secure',
      package: failure === 'package' ? 'different' : 'fixture.app', identity: '0123456789abcdef'});
    if (operation.startsWith('native-bind:')) {
      assert.deepEqual(operation.split(':').slice(1), keys.map((_, store) => encrypt(store, 0, 'PN_POPUP')));
      if (failure === 'bind') return 'wrong';
      return 'native-bound\t2';
    }
    if (operation.startsWith('native-get:')) {
      const store = Number(operation.split(':')[1]);
      if (failure === 'reentrant') {
        thread = 29;
        try { backend.read(store); } finally { thread = 11; }
      }
      return JSON.stringify({present: failure !== 'missing' && raw[store] !== null,
        plain_present: failure === 'legacy' || plain[store], encoded: raw[store]});
    }
    if (operation.startsWith('native-remove:')) {
      raw[Number(operation.split(':')[1])] = null;
      return JSON.stringify({commit: failure !== 'remove-commit'});
    }
    if (operation.startsWith('native-put:')) {
      const parts = operation.split(':'); raw[Number(parts[1])] = parts[2];
      return JSON.stringify({commit: failure !== 'commit'});
    }
    if (operation === 'native-unbind') return failure === 'unbind' ? 'wrong' : 'native-unbound\t2';
    throw new Error('unexpected Java route: ' + operation);
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../e2_1_pn_native_backend.js'), 'utf8'), context);
  const config = {mode, package: 'fixture.app', crypto_library: '/fixture/crypto',
    table: new Array(24).fill(1), iv_source: new Array(16).fill(2)};
  if (options.diagnostics !== undefined) config.diagnostics = options.diagnostics;
  const backend = context.createPNNativeBackend(config, invoke, {getExportByName: name => name});
  return {backend, raw, plain, encrypt, operations, config, context, buffers, functions, messages,
    streamCalls, streamOpens: () => streamOpens, streamCloses: () => streamCloses, closes: () => closes};
}

{
  const {backend, operations, closes, buffers} = fixture();
  assert.equal(backend.snapshot().started, false);
  assert.equal(Buffer.from(backend.read(1)).toString(), 'preferred');
  for (const text of ['new', '中文🙂', 'line\n<>&"', 'x'.repeat(4095)]) {
    backend.write(0, text);
    assert.equal(Buffer.from(backend.read(0)).toString(), text);
  }
  const snapshot = backend.snapshot();
  assert.equal(snapshot.derivations, 1); assert.equal(snapshot.transforms, 11);
  assert.equal(snapshot.errors.length, 0);
  assert.equal(operations.filter(value => value === 'derivation-source').length, 1);
  assert(!operations.some(value => value.startsWith('bridge-read') || value.startsWith('bridge-write')));
  assert.equal(backend.retire(), true); assert.equal(closes(), 1);
  assert(buffers[0].buffer.every(value => value === 0));
  assert(buffers[1].buffer.every(value => value === 0));
  assert.throws(() => backend.read(0)); assert.throws(() => backend.retire());
}
for (const failure of ['derive', 'open', 'bind', 'package', 'missing', 'legacy', 'reentrant']) {
  const {backend, closes, buffers} = fixture(failure);
  assert.throws(() => backend.read(1));
  assert(backend.snapshot().errors.length > 0);
  backend.retire();
  assert.equal(closes(), ['bind', 'missing', 'legacy', 'reentrant'].includes(failure) ? 1 : 0);
  assert(buffers[0].buffer.every(value => value === 0));
}
for (const value of ['!', 'AAA=', Buffer.alloc(16).toString('base64'), 'Zh==']) {
  const {backend, raw} = fixture(); raw[1] = value;
  assert.throws(() => backend.read(1)); assert.equal(raw[1], value);
  backend.retire();
}
for (const bytes of [Buffer.alloc(0), Buffer.from([0xff]), Buffer.from([0])]) {
  const {backend, raw, encrypt} = fixture(); raw[1] = encrypt(1, 1, bytes);
  const before = raw[1]; assert.throws(() => backend.read(1)); assert.equal(raw[1], before); backend.retire();
}
{
  const {backend, raw, encrypt} = fixture('commit');
  assert.throws(() => backend.write(0, 'changed'), /commit returned false/);
  assert.equal(raw[0], encrypt(0, 1, 'changed')); backend.retire();
}
{
  const {backend, closes, buffers} = fixture('unbind'); backend.read(1);
  assert.throws(() => backend.retire(), /unbind receipt/);
  assert.equal(closes(), 1); assert(buffers[0].buffer.every(value => value === 0));
}
{
  const {context, config} = fixture();
  for (const changed of [{mode: 'unknown'}, {table: [1]}, {package: ''}, {iv_source: [1]}, {crypto_library: 'relative'}]) {
    assert.throws(() => context.createPNNativeBackend({...config, ...changed}, () => '', {}));
  }
}
console.log('Native PN backend engineering fixture passed; not native evidence');
module.exports = {fixture};
