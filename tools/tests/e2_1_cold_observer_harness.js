'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fixture(fail) {
  const hooks = new Map();
  const calls = [];
  class Pointer {
    constructor(value) { this.value = value; }
    add(value) { return new Pointer(this.value + value); }
    sub(other) { return new Pointer(this.value - other.value); }
    compare(other) { return this.value - other.value; }
    toString() { return String(this.value); }
    readU32() { return this.value === 104 ? 8 : 0; }
    readPointer() { return new Pointer(200); }
    and() { return this; }
    isNull() { return false; }
    readUtf8String() { return 'PN_POPUP'; }
    readU8() { return 0; }
  }
  const module = {name: 'libg.so', base: new Pointer(1000), size: 5000};
  const context = {rpc: {exports: {}}, Process: {pointerSize: 8, getCurrentThreadId: () => 7,
    findRangeByAddress: () => ({protection: 'r-x'}),
    attachModuleObserver(observer) { observer.onAdded(module); }},
    Interceptor: {attach(target, callbacks) { hooks.set(target.value - 1000, callbacks); }}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../e2_1_return_observer.js'), 'utf8'), context);
  context.invoke = operation => {
    calls.push(operation);
    if (operation === 'install') {
      const nested = {returnAddress: new Pointer(3000)};
      hooks.get(21018548).onEnter.call(nested, [new Pointer(100)]);
      if (fail) throw new Error('real delegate failure fixture');
      return 'installed\t2';
    }
    return 'snapshot fixture';
  };
  const config = {jni_attach: 0x17d8ae8, dex_path: '/data/user/0/nullsroyale.rel.free/fixture.dex',
    optimized_directory: '/data/user/0/nullsroyale.rel.free/code_cache', install_at: 'first-native-bridge'};
  context.rpc.exports.configure(config);
  return {context, hooks, calls, Pointer};
}

for (const fail of [false, true]) {
  const {context, hooks, calls, Pointer} = fixture(fail);
  assert.equal(calls.length, 0);
  assert.equal(hooks.size, 2);
  assert.throws(() => context.rpc.exports.command('install'), /original bridge/);
  for (let index = 0; index < 2; ++index) {
    const invocation = {returnAddress: new Pointer(3000)};
    const hook = hooks.get(21022348);
    hook.onEnter.call(invocation, [new Pointer(100)]);
    hook.onLeave.call(invocation);
    if (!fail) hooks.get(2000).onEnter();
  }
  const snapshot = context.rpc.exports.snapshot();
  assert.equal(calls.filter(operation => operation === 'install').length, 1);
  assert.equal(snapshot.cold.reentrant_entries, 1);
  assert.equal(snapshot.cold.entries, 3);
  assert.equal(snapshot.cold.install_entry.entry, 1);
  assert.equal(snapshot.cold.installed, !fail);
  assert.equal(snapshot.cold.errors.length, fail ? 1 : 0);
  assert.equal(snapshot.cold.windows.length, fail ? 0 : 2);
  assert.equal(snapshot.no_vm_verified, false);
  for (const window of snapshot.cold.windows) assert.equal(window.after, window.before);
}
{
  const {context, hooks, Pointer} = fixture(false);
  const hook = hooks.get(21022348);
  const invocation = {returnAddress: new Pointer(3000)};
  hook.onEnter.call(invocation, [new Pointer(100)]);
  assert.equal(context.rpc.exports.snapshot().cold.windows[0].after, undefined);
  hooks.get(2000).onEnter();
  const window = context.rpc.exports.snapshot().cold.windows[0];
  assert.equal(window.completion, 'original callsite return instruction');
  const external = {returnAddress: new Pointer(9000)};
  hook.onEnter.call(external, [new Pointer(100)]);
  assert.equal(context.rpc.exports.snapshot().cold.external_windows, 1);
  assert.equal(context.rpc.exports.snapshot().cold.windows.length, 1);
}
console.log('Cold observer engineering fixture passed; not native evidence');
