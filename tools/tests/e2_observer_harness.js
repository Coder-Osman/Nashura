'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const memory = Buffer.alloc(8192);
let thread = 1;
const hooks = new Map();
class Pointer {
  constructor(offset) { this.offset = offset; }
  add(value) { return new Pointer(this.offset + value); }
  and() { return this; }
  sub(value) { return new Pointer(this.offset - value.offset); }
  isNull() { return this.offset === 0; }
  compare(other) { return this.offset - other.offset; }
  toString() { return '0x' + this.offset.toString(16); }
  toInt32() { return this.offset | 0; }
  readU8() { return memory.readUInt8(this.offset); }
  readU16() { return memory.readUInt16LE(this.offset); }
  readU32() { return memory.readUInt32LE(this.offset); }
  readU64() { return memory.readBigUInt64LE(this.offset); }
  readPointer() { return new Pointer(Number(this.readU64())); }
  readUtf8String(length) { return memory.subarray(this.offset, this.offset + length).toString('utf8'); }
}
const context = {Process: {getCurrentThreadId: () => thread, pointerSize: 8,
  findModuleByAddress: () => null, findRangeByAddress: () => ({base: new Pointer(0), size: memory.length, protection: 'rwx'}),
  attachModuleObserver() {}}, Interceptor: {attach(address, callbacks) { hooks.set(address.offset, callbacks); }},
  rpc: {exports: {}}, send() {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../e2_jni_config_observer.js'), 'utf8'), context);
context.rpc.exports.configure({engine_module: 'libg.so', functions: [], jni_names: [null, 'GetStringLength', 'GetByteArrayRegion'], sample_limit: 2});
memory.write('key\0ignored', 100);
assert.equal(context.readUtf8(new Pointer(100), 1024).value, 'key');
assert.equal(context.decodeModifiedUtf8([0xc0, 0x80]).value, '\0');
assert.equal(context.decodeModifiedUtf8([0xf0, 0x90, 0x80, 0x80]).status, 'invalid_mutf8');
memory[200] = 6; memory.write('abc\0', 201);
assert.equal(context.readCppString(new Pointer(200)).value, 'abc');
memory[240] = 1; memory.writeBigUInt64LE(4n, 248); memory.writeBigUInt64LE(300n, 256); memory.write('long\0', 300);
assert.equal(context.readCppString(new Pointer(240)).value, 'long');
memory.writeUInt32LE(3, 404); memory.write('xyz\0', 408);
assert.equal(context.readEngineString(new Pointer(400)).value, 'xyz');
memory.writeBigUInt64LE(1000n, 800); memory.writeBigUInt64LE(2000n, 1008); memory.writeBigUInt64LE(2100n, 1016);
context.watchJniTable(new Pointer(800));
const invocation = {returnAddress: new Pointer(700)};
hooks.get(2000).onEnter.call(invocation, [800, 500, 0, 0, 0].map(value => new Pointer(value)));
hooks.get(2000).onLeave.call(invocation, new Pointer(3));
const region = {returnAddress: new Pointer(700)};
hooks.get(2100).onEnter.call(region, [800, 500, 0, 3, 600].map(value => new Pointer(value)));
memory.write('out', 600);
hooks.get(2100).onLeave.call(region, new Pointer(0));
const snapshot = context.rpc.exports.snapshot();
assert.equal(snapshot.jni_counts[0].samples[0].result.length, 3);
assert.equal(snapshot.jni_counts[1].samples[0].result.fnv1a32, context.byteHash(new Pointer(600), 3).fnv1a32);
context.watchFunction(new Pointer(3000), 'step', {phase: 'first_tick'});
const step = {returnAddress: new Pointer(700), context: {x8: new Pointer(0)}};
hooks.get(3000).onEnter.call(step, []);
assert.equal(context.phase(1), 'first_tick'); thread = 2;
assert.equal(context.phase(2), 'process_start'); thread = 1;
hooks.get(3000).onLeave.call(step, new Pointer(0));
assert.equal(context.phase(1), 'process_start');
console.log('E2 observer engineering fixture passed; not native evidence');
