'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
let thread = 1;
const callbacks = new Map();
const pointer = name => ({toString: () => name, sub() { return this; }});
const context = {
  Process: {getCurrentThreadId: () => thread, pointerSize: 8,
    findModuleByAddress: () => null, findRangeByAddress: () => ({protection: 'r-x'}),
    attachModuleObserver() {}, enumerateThreads: () => [{id: 1, name: 'control'}, {id: 2, name: 'Mainloop'}]},
  Instruction: {parse() { throw new Error('synthetic unavailable instruction'); }},
  Interceptor: {attach(address, handlers) {
    if (address.toString() === 'failed') throw new Error('synthetic short stub');
    callbacks.set(address.toString(), handlers);
  }}, rpc: {exports: {}}, send() {}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../e0_dependency_observer.js'), 'utf8'), context);
context.rpc.exports.configure({sample_limit: 1});
context.rpc.exports.phase('full_trace');
context.hook(pointer('step'), 'Step', {track_scope: true, phase: 'sustained_early'});
context.hook(pointer('setup'), 'GameStateSetup', {track_scope: true});
context.hook(pointer('jni'), 'JNI.synthetic');
context.hook(pointer('failed'), 'short_stub');
function enter(name) {
  const invocation = {returnAddress: pointer('synthetic_caller')};
  callbacks.get(name).onEnter.call(invocation, [pointer('arg0'), pointer('arg1'), pointer('arg2')]);
  return invocation;
}
const step = enter('step');
const setup = enter('setup');
const sameThread = enter('jni');
callbacks.get('jni').onLeave.call(sameThread, pointer('return'));
thread = 2;
const concurrent = enter('jni');
callbacks.get('jni').onLeave.call(concurrent, pointer('return'));
thread = 1;
callbacks.get('setup').onLeave.call(setup, pointer('return'));
callbacks.get('step').onLeave.call(step, pointer('return'));
const snapshot = JSON.parse(JSON.stringify(context.rpc.exports.snapshot()));
const setupRow = snapshot.counts.find(row => row.labels.includes('GameStateSetup'));
assert.equal(setupRow.phase, 'sustained_early');
assert.deepEqual(setupRow.native_scopes, ['Step', 'GameStateSetup']);
const rows = snapshot.counts.filter(row => row.labels.includes('JNI.synthetic'));
assert.equal(rows.length, 2);
assert.deepEqual(rows.find(row => row.thread === 1).native_scopes, ['Step', 'GameStateSetup']);
assert.deepEqual(rows.find(row => row.thread === 2).native_scopes, []);
assert.equal(rows.find(row => row.thread === 2).phase, 'full_trace');
assert.match(rows.find(row => row.thread === 2).attribution, /overlap only/);
assert.equal(snapshot.errors[0].count, null);
assert.equal(snapshot.coverage.find(row => row.label === 'short_stub').status, 'failed');
assert.equal(snapshot.attribution_version, 2);
console.log('observer attribution engineering fixture passed');
