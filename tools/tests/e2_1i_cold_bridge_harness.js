'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function fixture(failure, gate) {
  const order = [];
  let lifecycle;
  const library = {};
  const dispatchLibrary = {};
  const context = {rpc: {exports: {}},
    Module: {load(name) {
      assert.equal(order.includes('module-observer'), false);
      if (name === 'fixture-library') { order.push('library'); return library; }
      assert.equal(name, '/data/app/fixture/lib/arm64/libe21dispatchgate.so');
      order.push('dispatch-library');
      return dispatchLibrary;
    }},
    Process: {getCurrentThreadId: () => 7, attachModuleObserver(observer) {
      order.push('module-observer'); observer.onAdded({name: 'libg.so'});
    }},
    createPNBridge(config, invoke, callbacks, loaded, loadedDispatch) {
      order.push('bridge');
      assert.equal(loaded, library);
      assert.equal(loadedDispatch, gate ? dispatchLibrary : null);
      lifecycle = callbacks;
      return {snapshot: () => ({installed: true, events: []})};
    }};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../e2_1_return_observer.js'), 'utf8'), context);
  context.invoke = operation => {
    order.push(operation);
    if (failure === 'reentrant') lifecycle.entry(true);
    if (failure === 'exception') throw new Error('fixture backend failure');
    return failure === 'receipt' ? 'wrong receipt' : 'installed\t2';
  };
  context.rpc.exports.configure({jni_attach: 0x17d8ae8,
    dex_path: '/data/user/0/nullsroyale.rel.free/fixture.dex',
    optimized_directory: '/data/user/0/nullsroyale.rel.free/code_cache',
    install_at: 'first-native-bridge', enable_pn_backend: true,
    pn_bridge: {install_at: 'first-native-bridge', library: 'fixture-library',
      ...(gate ? {dispatch_gate: gate} : {})}});
  assert.deepEqual(order, ['library', ...(gate ? ['dispatch-library'] : []), 'module-observer', 'bridge']);
  assert.equal(context.rpc.exports.snapshot().cold.attempted, false);
  return {context, order, lifecycle};
}
{
  const gate = {mode: 'native-serial-v1', library: '/data/app/fixture/lib/arm64/libe21dispatchgate.so'};
  const {context, lifecycle} = fixture(undefined, gate);
  lifecycle.entry(true);
  assert.equal(context.rpc.exports.snapshot().cold.installed, true);
  for (const invalid of [{...gate, mode: 'unknown'}, {...gate, library: '/tmp/fixture.so'},
                         {...gate, library: null}]) {
    assert.throws(() => fixture(undefined, invalid), /explicit native serial dispatch gate identity required/);
  }
}

{
  const {context, order, lifecycle} = fixture();
  lifecycle.entry(false);
  lifecycle.entry(true);
  const cold = context.rpc.exports.snapshot().cold;
  assert.equal(cold.entries, 2);
  assert.equal(cold.install_entry.entry, 1);
  assert.equal(cold.installed, true);
  assert.equal(cold.installing, false);
  assert.equal(cold.reentrant_entries, 0);
  assert.equal(order.filter(value => value === 'install').length, 1);
  assert.throws(() => context.rpc.exports.command('install'), /original bridge/);
}
for (const failure of ['receipt', 'exception', 'reentrant']) {
  const {context, order, lifecycle} = fixture(failure);
  assert.throws(() => lifecycle.entry(false));
  assert.throws(() => lifecycle.entry(false), /not installed/);
  const cold = context.rpc.exports.snapshot().cold;
  assert.equal(cold.installed, false);
  assert.equal(cold.installing, false);
  assert.equal(cold.errors.length, 1);
  assert.equal(cold.reentrant_entries, failure === 'reentrant' ? 1 : 0);
  assert.equal(order.filter(value => value === 'install').length, 1);
}
console.log('Cold PN bridge engineering fixture passed; not native evidence');
