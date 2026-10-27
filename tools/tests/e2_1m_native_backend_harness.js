'use strict';
const assert = require('node:assert/strict');
const {fixture} = require('./e2_1k_native_backend_harness.js');
const create = failure => fixture(failure, 'persistent-pn-edges-v1');
for (const plain of [false, true]) {
  const state = create(); state.raw[0] = null; state.plain[0] = plain;
  assert.equal(Buffer.from(state.backend.read(0)).length, 0);
  assert.equal(state.streamCalls[0], 0);
  assert(!state.operations.includes('native-remove:0'));
  assert.equal(Buffer.from(state.backend.read(1)).toString(), 'preferred');
  assert.equal(state.backend.retire(), true);
  assert.equal(state.streamCloses(), 2); assert.equal(state.closes(), 1);
}
for (const input of ['', '%%%', 'YmFk', Buffer.alloc(16).toString('base64')]) {
  const state = create(); state.raw[0] = input;
  assert.equal(Buffer.from(state.backend.read(0)).length, 0);
  assert.equal(state.raw[0], null);
  assert.equal(state.operations.filter(value => value === 'native-remove:0').length, 1);
  assert.equal(state.streamCalls[0], 1); assert.equal(state.streamCalls[1], 0);
  assert.equal(state.backend.snapshot().decoder_failures[0], 1);
  state.backend.retire();
}
{
  const state = create(); state.raw[0] = state.encrypt(0, 1, '');
  assert.equal(Buffer.from(state.backend.read(0)).length, 0);
  assert.equal(state.raw[0], null);
  assert.equal(state.backend.snapshot().decoder_failures[0], 0);
  state.backend.write(1, ''); assert.equal(state.raw[1], null);
  state.backend.write(1, '中文🙂');
  assert.equal(Buffer.from(state.backend.read(1)).toString(), '中文🙂');
  state.backend.retire();
}
for (const input of ['!', 'Zh==', 'AAA']) {
  const state = create(); state.raw[0] = input;
  assert.throws(() => state.backend.read(0));
  assert.equal(state.raw[0], input); assert.equal(state.streamCalls[0], 0);
  state.backend.retire();
}
for (const input of [Buffer.from([0]), Buffer.from([0xff])]) {
  const state = create(); state.raw[0] = state.encrypt(0, 1, input);
  const original = state.raw[0]; assert.throws(() => state.backend.read(0));
  assert.equal(state.raw[0], original); state.backend.retire();
}
for (const failure of ['stream-open-0', 'stream-open-1', 'stream-status', 'stream-partial', 'bind', 'unbind']) {
  const state = create(failure);
  if (failure === 'unbind') {
    state.backend.read(0); assert.throws(() => state.backend.retire());
  } else {
    assert.throws(() => state.backend.read(0)); state.backend.retire();
  }
  assert.equal(state.streamCloses(), failure === 'stream-open-0' ? 0 : failure === 'stream-open-1' ? 1 : 2);
  assert.equal(state.closes(), 1);
  assert(state.buffers[0].buffer.every(value => value === 0));
  assert(state.buffers[1].buffer.every(value => value === 0));
}
{
  const state = create('remove-commit'); state.raw[0] = '%%%';
  assert.throws(() => state.backend.read(0), /remove commit returned false/);
  assert.equal(state.raw[0], null); state.backend.retire();
}
console.log('Persistent PN backend engineering fixture passed; not native state-equivalence evidence');
