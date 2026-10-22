const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const hooks = new Map(), messages = [];
const memory = new Map();
class Pointer {
  constructor(value) { this.value = value; }
  add(n) { return new Pointer(this.value + n); }
  sub(p) { return new Pointer(this.value - p.value); }
  equals(p) { return this.value === p.value; }
  isNull() { return this.value === 0; }
  toString() { return String(this.value); }
  toInt32() { return this.value; }
  toUInt32() { return this.value >>> 0; }
  readS32() { assert(memory.has(this.value), 'unexpected memory read'); return memory.get(this.value); }
  readU8() { return this.readS32() & 255; }
}
const p = n => new Pointer(n);
const sandbox = {Memory: {alloc: () => p(0)}, Module: {getGlobalExportByName: () => p(0)},
  NativeFunction: function () { return () => 0; },
  Interceptor: {attach: (address, callbacks) => hooks.set(address.value, callbacks)},
  send: message => messages.push(message), epoch: 1, untag: v => v,
  pointer: (address, off) => p(address.add(off).readS32()),
  vector: () => { throw Error('unexpected queue read'); }};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') + '\nthis.Capture = LiveTimingCapture;', sandbox);
const capture = new sandbox.Capture({base: p(0)}, null);
capture.now = () => '1000';
capture.install();
const submit = hooks.get(0xc893d0), validate = hooks.get(0xd30a00), outbound = hooks.get(0xc89778);
const c = {account: p(10), battle: p(20), player: p(30), owner: 0, tick: 200};
const command = p(1000), reference = p(2000);
capture.localCommand = () => ({c, command, fields: {command_id: 1, card_id: 28000000}});
capture.ownResources = () => ({elixir_raw: 50000, reserved_elixir: 4, pending_count: 1});

// Foreign commands never dereference memory or emit local diagnostics.
validate.onEnter.call({}, [p(999999), c.battle, p(1)]);
assert.equal(messages.length, 0);

function attempt({code, accepted, destroyed, sent, state}) {
  messages.length = 0;
  memory.clear();
  memory.set(reference.value, command.value);
  memory.set(command.value + 0x28, 3500);
  memory.set(command.value + 0x2c, 8500);
  const frame = {};
  submit.onEnter.call(frame, [c.account, reference]);
  if (state) frame.timedCommand.fields.submit_state = state;
  if (code !== undefined) {
    const v = {};
    validate.onEnter.call(v, [command, c.battle, p(1)]);
    validate.onLeave.call(v, p(code));
  }
  if (sent) outbound.onEnter.call({}, [c.account, reference, p(200)]);
  // The destructor has freed command memory before submit returns.
  memory.delete(command.value + 0x28);
  memory.delete(command.value + 0x2c);
  if (destroyed) memory.set(reference.value, 0);
  submit.onLeave.call(frame, p(accepted ? 1 : 0));
  assert(!messages.some(m => m.event === 'timing_error'));
  assert.equal(capture.commands.size, 0);
  return messages.at(-1);
}
let result = attempt({code: 13, accepted: false, destroyed: true});
assert.equal(result.outcome, 'validation_rejected');
assert.equal(result.validations[0].result, 13);
assert.equal(result.validations[0].reserved_elixir, 4);
assert.equal(result.command_destroyed, true);
result = attempt({accepted: false, destroyed: false});
assert.equal(result.outcome, 'unknown_or_queued');
result = attempt({code: 0, accepted: true, destroyed: false, sent: true});
assert.equal(result.outcome, 'outbound');
assert.equal(result.outbound_seen, true);
result = attempt({accepted: false, destroyed: true});
assert.equal(result.outcome, 'unknown_or_queued');
// Inconsistent evidence must never grant recovery.
result = attempt({code: 13, accepted: false, destroyed: true, sent: true});
assert.equal(result.outcome, 'outbound');
const stoppedState = {battle_stopped: 1, defer_enabled: 0, queued_commands: 0};
result = attempt({accepted: false, destroyed: true, state: stoppedState});
assert.equal(result.outcome, 'battle_sync_stopped');
result = attempt({accepted: false, destroyed: false, state: stoppedState});
assert.equal(result.outcome, 'unknown_or_queued');
result = attempt({accepted: false, destroyed: true,
  state: {...stoppedState, defer_enabled: 1}});
assert.equal(result.outcome, 'unknown_or_queued');

// Capture the first fault at its shared helper. Registers are read before the
// helper's prologue, and foreign battles/debug report calls are ignored.
const live = {battle: p(0x5000), world: p(0x6000), owner: 0, tick: 4300};
capture.context = () => live;
memory.clear();
for (const [off, value] of [[0x60, 4300], [0x64, 4299], [0x1bc, 4280], [0x1e9, 1], [0x1eb, 1]])
  memory.set(live.battle.value + off, value);
const syncHook = hooks.get(0x106482c);
const registers = {x11: p(0xfedcba98), x20: p(234), x21: p(4300), x24: p(567)};
function fault(offset, regs = registers, battle = live.battle) {
  messages.length = 0;
  const before = [...memory];
  syncHook.onEnter.call({returnAddress: p(offset), context: regs}, [battle]);
  assert.deepEqual([...memory], before);
  assert(!messages.some(m => m.event === 'timing_error'));
  return messages[0];
}
result = fault(0x1064fb8);
assert.equal(result.cause, 'checksum_mismatch_on_sync_update');
assert.equal(result.checksum_tick, 4300);
assert.equal(result.local_checksum, 0xfedcba98);
assert.equal(result.received_checksum, 234);
result = fault(0x106454c, {...registers, x21: p(123)});
assert.equal(result.cause, 'checksum_mismatch_during_step');
assert.equal(result.checksum_tick, 4300);
assert.equal(result.local_checksum, 123);
assert.equal(result.received_checksum, 567);
assert.equal(fault(0x1063150).cause, 'clock_lead_limit_exceeded');
assert.equal(fault(0x1064708).cause, 'explicit_out_of_sync');
assert.equal(fault(0xd385ec), undefined);
assert.equal(fault(0x106454c, registers, p(0x9999)), undefined);

// State transitions are observable even with a frozen tick; periodic samples
// remain bounded to one per second and reset for the next epoch.
messages.length = 0;
let stamp = 100000000000n;
capture.now = () => stamp.toString();
capture.observeBattle(live);
stamp += 100000000n;
capture.observeBattle(live);
assert.equal(messages.length, 1);
memory.set(live.battle.value + 0x1e9, 0);
capture.observeBattle(live);
assert.equal(messages.length, 2);
assert.equal(messages[1].battle_sync.stopped, 0);
stamp += 1000000000n;
capture.observeBattle(live);
assert.equal(messages.length, 3);
sandbox.epoch = 2;
capture.observeBattle(live);
assert.equal(messages.length, 4);
console.log('local command diagnostic fixtures passed');
