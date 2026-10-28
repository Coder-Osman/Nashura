'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const hooks = new Map();
const stringPointer = {
  and() { return this; },
  readU8() { return 0; },
  add() { return this; },
  isNull() { return false; }
};
const arg = value => ({toUInt32: () => value, and: () => ({add: () => ({readS32: () => 158})})});
const moduleObject = {name: 'libg.so', base: {add: offset => offset}};
const context = {
  ptr: () => ({}),
  Process: {
    getCurrentThreadId: () => 42,
    findRangeByAddress: () => ({protection: 'r-x'}),
    findModuleByName: () => null,
    attachModuleObserver: observer => observer.onAdded(moduleObject)
  },
  Interceptor: {attach: (address, callbacks) => hooks.set(address, callbacks)},
  rpc: {exports: {}}
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
context.rpc.exports.install();

const invocation = () => {
  const value = {returnAddress: {sub: () => 'caller'}};
  Object.defineProperty(value, 'threadId', {value: 42, writable: false});
  return value;
};
const batch = invocation();
hooks.get(0xe1bdcc).onEnter.call(batch, [arg(0), arg(0), arg(0), arg(0), arg(0), arg(0), arg(39)]);
const getter = invocation();
hooks.get(0xdcb6fc).onEnter.call(getter);
hooks.get(0xdcb6fc).onLeave.call(getter, arg(2));
hooks.get(0xe1bdcc).onLeave.call(batch);
const file = invocation();
hooks.get(0x12e03e4).onEnter.call(file, [arg(0), stringPointer]);
hooks.get(0x12e03e4).onLeave.call(file, {isNull: () => true});
const trace = context.rpc.exports.snapshot();
assert.equal(trace.errors.length, 0);
assert.equal(trace.activeScopes, 0);
assert.equal(trace.activeBatchScopes, 0);
assert.equal(trace.events.filter(event => event.kind === 'table_batch').length, 1);
assert.equal(trace.events.filter(event => event.kind === 'entry_type').length, 1);
assert.equal(trace.events.filter(event => event.kind === 'filesystem_open').length, 1);
