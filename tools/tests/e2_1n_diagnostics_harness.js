'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {fixture} = require('./e2_1k_native_backend_harness.js');

{
  const state = fixture('reentrant', 'persistent-pn-edges-v1', {diagnostics: 'call-timeline-v1'});
  assert.throws(() => state.backend.read(1), /bounded serial/);
  const snapshot = state.backend.snapshot();
  const rejection = snapshot.diagnostics.events.find(row => row.edge === 'rejected');
  assert.equal(rejection.thread, 29);
  assert.equal(rejection.owner.thread, 11);
  assert.equal(rejection.owner.stage, 'jni:native-get');
  assert.equal(rejection.owner.call, 1);
  assert.equal(rejection.busy, true);
  assert.equal(snapshot.diagnostics.busy, false);
  assert.equal(snapshot.diagnostics.owner, null);
  assert.equal(snapshot.diagnostics.dropped, 0);
  assert.equal(state.messages.length, snapshot.diagnostics.events.length);
  assert(snapshot.diagnostics.events.some(row => row.edge === 'stage-throw'));
  assert(snapshot.diagnostics.events.some(row => row.edge === 'call-throw'));
  const serialized = JSON.stringify(state.messages);
  for (const secret of ['0123456789abcdef', 'preferred', state.raw[1]]) assert(!serialized.includes(secret));
  state.backend.retire();
}
{
  const state = fixture(undefined, 'persistent-pn-edges-v1', {diagnostics: 'call-timeline-v1'});
  for (let index = 0; index < 700; index++) assert.equal(Buffer.from(state.backend.read(1)).toString(), 'preferred');
  const diagnostic = state.backend.snapshot().diagnostics;
  assert.equal(diagnostic.events.length, 4096);
  assert(diagnostic.dropped > 0);
  assert.equal(diagnostic.owner, null);
  assert.equal(diagnostic.busy, false);
  state.backend.retire();
}
assert.throws(() => fixture(undefined, 'persistent-pn-edges-v1', {diagnostics: 'unknown'}), /unknown PN diagnostics/);
assert.equal(fixture().backend.snapshot().diagnostics, undefined);

{
  const messages = [];
  let thread = 11;
  const context = {Process: {getCurrentThreadId: () => thread}, send: row => messages.push(row)};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../e2_1_pn_bridge.js'), 'utf8'), context);
  assert.throws(() => context.createPNDispatchDiagnostics('unknown', () => 'fixture'), /unknown PN dispatch/);
  const disabled = context.createPNDispatchDiagnostics(undefined, () => 'fixture');
  const hidden = disabled.enter(false);
  disabled.step(hidden, 'read', () => 'private text');
  disabled.leave(hidden);
  assert.equal(disabled.snapshot(), undefined);
  assert.equal(messages.length, 0);
  const diagnostic = context.createPNDispatchDiagnostics('entry-return-v1', () => 'fixture');
  const first = diagnostic.enter(true);
  diagnostic.step(first, 'native-write', () => {
    thread = 29;
    const second = diagnostic.enter(false);
    assert.throws(() => diagnostic.step(second, 'native-read', () => { throw new Error('private text'); }));
    diagnostic.leave(second);
    thread = 11;
  });
  diagnostic.leave(first);
  assert.equal(messages.length, 8);
  assert.equal(messages[4].detail.edge, 'throw');
  assert.equal(messages[4].detail.thread, 29);
  assert.equal(messages[4].detail.call, 2);
  assert.equal(JSON.stringify(messages).includes('private text'), false);
  for (let index = 0; index < 2100; index++) diagnostic.leave(first);
  for (let index = 0; index < 2100; index++) diagnostic.leave(first);
  assert.equal(diagnostic.snapshot().events.length, 4096);
  assert(diagnostic.snapshot().dropped > 0);
}

class Address {
  constructor(value, kind = 'address') { this.value = value; this.kind = kind; }
  add(amount) { return new Address(this.value + amount, this.kind); }
  sub(other) { return new Address(this.value - other.value); }
  compare(other) { return Math.sign(this.value - other.value); }
  and() { return this; }
  isNull() { return false; }
  readU32() { return this.kind === 'key' ? 8 : 0; }
  readPointer() { return new Address(0, 'text'); }
  readUtf8String() { return 'PN_POPUP'; }
  readU8() { return 0; }
  toString() { return '0x' + this.value.toString(16); }
}
function timelineFixture(executable = true) {
  const hooks = [], messages = [];
  let flushed = 0;
  let thread = 11;
  const engine = {name: 'libg.so', base: new Address(0x10000000), size: 30000000};
  const context = {
    rpc: {exports: {}}, send: message => messages.push(message),
    Process: {arch: 'arm64', getCurrentThreadId: () => thread,
      findRangeByAddress: () => executable ? {protection: 'r-x'} : {protection: 'rw-'},
      attachModuleObserver(observer) { observer.onAdded(engine); return {detach() { throw new Error('unexpected detach'); }}; }},
    Interceptor: {attach(address, callbacks) { hooks.push({address, callbacks}); return {}; },
      flush() { flushed = hooks.length; }}
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../e2_1_pn_timeline.js'), 'utf8'), context);
  return {api: context.rpc.exports, hooks, messages, engine, flushed: () => flushed,
    thread(value) { thread = value; }};
}
const config = {read_bridge: 21022348, write_bridge: 21018548, max_events: 32};
{
  const state = timelineFixture();
  state.api.configure(config);
  const first = {returnAddress: state.engine.base.add(100)};
  const second = {returnAddress: new Address(0)};
  state.hooks[0].callbacks.onEnter.call(first, [new Address(0, 'key')]);
  assert.equal(state.flushed(), 3);
  state.thread(29);
  state.hooks[1].callbacks.onEnter.call(second, [new Address(0, 'key')]);
  assert.equal(state.flushed(), 4);
  let snapshot = state.api.snapshot();
  assert.equal(snapshot.events[0].caller, 'libg.so+0x64');
  assert.equal(snapshot.events[1].caller, 'external-probe');
  assert.deepEqual(Array.from(snapshot.events[1].overlaps), [1]);
  state.hooks.find(row => row.address.compare(second.returnAddress) === 0).callbacks.onEnter();
  state.thread(11);
  state.hooks.find(row => row.address.compare(first.returnAddress) === 0).callbacks.onEnter();
  snapshot = state.api.snapshot();
  assert.equal(snapshot.active.length, 0);
  assert.equal(snapshot.calls, 2);
  assert.equal(snapshot.errors.length, 0);
  assert.equal(snapshot.dropped, 0);
  assert.equal(snapshot.retained_listeners, 2);
  assert.equal(snapshot.retained_return_sites, 2);
  assert.equal(snapshot.pending_threads, 0);
  assert.equal(snapshot.schema, 'e2.1n-original-pn-timeline.v4');
  assert.equal(snapshot.module_observer_retained, true);
  for (let index = 0; index < 20; index++) {
    const call = {returnAddress: state.engine.base};
    state.hooks[0].callbacks.onEnter.call(call, [new Address(0, 'key')]);
    state.hooks.find(row => row.address.compare(call.returnAddress) === 0).callbacks.onEnter();
  }
  snapshot = state.api.snapshot();
  assert.equal(snapshot.events.length, 32);
  assert.equal(snapshot.dropped, 12);
  assert.equal(snapshot.active.length, 0);
  assert.equal(state.messages.length, 32);
  assert.throws(() => state.api.configure(config), /explicit bounded/);
}
assert.throws(() => timelineFixture(false).api.configure(config), /not executable/);
{
  const state = timelineFixture();
  state.api.configure({...config, max_events: 4096});
  const site = state.engine.base.add(128);
  const enter = thread => {
    state.thread(thread);
    state.hooks[0].callbacks.onEnter.call({returnAddress: site}, [new Address(0, 'key')]);
  };
  enter(11);
  enter(11);
  enter(29);
  const leave = state.hooks.find(row => row.address.compare(site) === 0).callbacks.onEnter;
  state.thread(47);
  leave();
  assert.equal(state.api.snapshot().events.length, 3);
  state.thread(11);
  leave();
  assert.equal(state.api.snapshot().events.at(-1).call, 2);
  leave();
  assert.equal(state.api.snapshot().events.at(-1).call, 1);
  let snapshot = state.api.snapshot();
  assert.equal(snapshot.active.length, 1);
  assert.equal(snapshot.active[0].thread, 29);
  assert.equal(snapshot.pending_threads, 1);
  assert.equal(snapshot.retained_return_sites, 1);
  state.thread(29);
  leave();
  snapshot = state.api.snapshot();
  assert.equal(snapshot.events.at(-1).call, 3);
  assert.equal(snapshot.active.length, 0);
  assert.equal(snapshot.pending_threads, 0);
}
{
  const state = timelineFixture();
  state.api.configure({...config, max_events: 4096});
  const first = state.engine.base.add(128);
  const second = state.engine.base.add(256);
  for (const site of [first, second]) {
    state.hooks[0].callbacks.onEnter.call({returnAddress: site}, [new Address(0, 'key')]);
  }
  const leaveAt = site => state.hooks.find(row => row.address.compare(site) === 0).callbacks.onEnter();
  leaveAt(first);
  assert.equal(state.api.snapshot().events.length, 2);
  assert.equal(state.api.snapshot().active.length, 2);
  leaveAt(second);
  assert.equal(state.api.snapshot().events.at(-1).call, 2);
  leaveAt(first);
  assert.equal(state.api.snapshot().events.at(-1).call, 1);
  assert.equal(state.api.snapshot().active.length, 0);
}
{
  const state = timelineFixture();
  state.api.configure({...config, max_events: 4096});
  for (let index = 0; index < 65; index++) {
    const site = state.engine.base.add(1000 + index * 4);
    state.hooks[0].callbacks.onEnter.call({returnAddress: site}, [new Address(0, 'key')]);
    if (index < 64) state.hooks.find(row => row.address.compare(site) === 0).callbacks.onEnter();
  }
  const snapshot = state.api.snapshot();
  assert.equal(snapshot.retained_return_sites, 64);
  assert.equal(snapshot.calls, 64);
  assert.equal(snapshot.errors.length, 1);
  assert.match(snapshot.errors[0].error, /bounded executable coverage/);
  assert.equal(snapshot.errors[0].completion, 'unknown');
  assert.equal(snapshot.active.length, 0);
}
for (const change of [{read_bridge: 0}, {write_bridge: 0}, {max_events: 31}, {max_events: 4097}]) {
  assert.throws(() => timelineFixture().api.configure({...config, ...change}), /explicit bounded/);
}
console.log('PN concurrency diagnostic fixtures passed; no native concurrency equivalence claimed');
