// Exercise the production reader against an exact-layout public memory fixture.
// Unmapped reads throw, including any unexpected access outside this entity.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const memory = new Map();
class Pointer {
  constructor(address) { this.address = Number(address); }
  add(offset) { return new Pointer(this.address + offset); }
  and(mask) { return new Pointer(BigInt(this.address) & BigInt(mask)); }
  equals(other) { return this.address === other.address; }
  isNull() { return this.address === 0; }
  toString() { return '0x' + this.address.toString(16); }
  read() {
    assert.ok(memory.has(this.address), 'unexpected memory read: ' + this);
    return memory.get(this.address);
  }
  readS32() { return this.read() | 0; }
  readU32() { return this.read() >>> 0; }
  readPointer() { return new Pointer(this.read()); }
  readUtf8String(length) { return this.read().slice(0, length); }
}
const base = 0x10000000, entity = 0x1000, slots = 0x2000, hp = 0x3000;
const effects = 0x4000, data = 0x5000, attack = 0x6000, name = 0x8000;
const set = (address, value) => memory.set(address, value);
for (const [offset, value] of [
  [8, 5000060], [0x18, slots], [0x20, 4], [0x24, 4], [0x30, 12],
  [0x48, data], [0x78, 1], [0x7c, 6000], [0x80, 20000], [0xac, 203000088],
  [0x15c, 0], [0x160, 0],
]) set(entity + offset, value);
for (const [index, value] of [attack, 0, hp, effects].entries()) {
  set(slots + index * 8, value);
  if (value) set(value + 8, entity);
}
set(hp, base + 0x18a1ec0);
set(base + 0x18a1ec0 + 0x20, base + 0xf62f80);
set(hp + 0x10, 2559997); set(hp + 0x14, 2559997);
set(effects + 0x20, 0); set(effects + 0x24, 0); set(effects + 0x34, 0);
set(name, 'TombstoneHero_visual_dummy');
set(data + 0x2c, memory.get(name).length); set(data + 0x30, name);
set(data + 0x40, 2573438603);
set(data + 0x418, 9999999); set(data + 0x490, 1000); set(data + 0x6a0, 0);
set(attack, base + 0x18a1dd0); set(attack + 0x10, 0);
set(attack + 0x20, -1); set(attack + 0x24, 0); set(attack + 0x28, 9999999);
const context = vm.createContext({ptr: value => new Pointer(value), rpc: {exports: {}}});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
vm.runInContext(`libBase = ptr(${base}); phaseCapture = {snapshot: () => ({hooks_attested: false})}`, context);
const read = () => vm.runInContext(`visibleEntity(ptr(${entity}), 0)`, context);
const result = read().entity;
assert.equal(result.hp, 2559997);
assert.equal(result.max_hp, 2559997);
assert.equal(result.native_data_id, 2573438603);
assert.equal(result.native_data_name, 'TombstoneHero_visual_dummy');
assert.equal(result.phase.hit_speed_ms, 9999999);
assert.equal(result.phase.load_remaining_ms, 9999999);

set(hp + 0x10, 0x7fffffff); set(hp + 0x14, 0);
assert.equal(read().entity.hp, 0x7fffffff);
assert.equal(read().entity.max_hp, 0);
set(hp + 0x14, -1);
assert.throws(read, /invalid HP: entity=5000060.*max_hp=-1/);
set(hp + 0x14, 2559997);
set(hp, base + 0x18a1dd0);
assert.throws(read, /invalid HP component type/);
set(hp, base + 0x18a1ec0);
set(base + 0x18a1ec0 + 0x20, base + 0xf62f84);
assert.throws(read, /invalid HP component type/);
set(base + 0x18a1ec0 + 0x20, base + 0xf62f80);
set(hp + 8, entity + 1);
assert.throws(read, /component owner mismatch/);
set(hp + 8, entity);
set(data + 0x418, -1);
assert.throws(read, /invalid public runtime field: offset=0x418 value=-1/);
set(data + 0x418, 9999999);
const movement = 0x9000;
set(slots + 8, movement); set(movement + 8, entity);
set(movement, base + 0x18a1f38);
set(movement + 0x1e0, 10080); set(data + 0x43c, 200);
assert.equal(read().entity.phase.classic_charge_progress, 10080);
set(movement + 0x1e0, -2);
assert.throws(read, /invalid public runtime field: offset=0x1e0 value=-2/);
set(movement + 0x1e0, -1);
assert.equal(read().entity.phase.classic_charge_progress, -1);
set(effects + 0x20, 1); set(effects + 0x24, 1); set(effects + 0x34, 1);
assert.equal(read().omitted, 'visibility');
set(hp + 0x10, 0);
assert.equal(read().omitted, 'dead');
console.log('native health, timer, identity and visibility regression passed');

// A live vector may compact during entity enumeration. Such samples must be
// discarded; distinct entities sharing an ID must never be silently merged.
const collection = 0xa000, objects = 0xb000;
set(collection + 8, objects); set(collection + 16, 4); set(collection + 20, 2);
set(objects, entity); set(objects + 8, entity + 0x100);
vm.runInContext(`
  const fixtureCollection = ptr(${collection});
  const fixtureObjects = vector(fixtureCollection, 8, 2048);
  const fixtureMembers = [ptr(${entity}), ptr(${entity + 0x100})];
  const fixtureIdentities = [
    {id: 5000010, index: 0, pointer: 'first', name: 'A'},
    {id: 5000011, index: 1, pointer: 'second', name: 'B'}];
`, context);
const verify = () => vm.runInContext(
  'verifyEntityCollection(fixtureCollection, fixtureObjects, fixtureMembers, fixtureIdentities)', context);
verify();
set(objects, entity + 0x100);
assert.throws(verify, /entity collection changed during read: index=0/);
set(objects, entity);
set(collection + 20, 1);
assert.throws(verify, /entity collection changed during read: count=2->1/);
set(collection + 20, 2);
set(collection + 8, objects + 0x100);
assert.throws(verify, /entity collection changed during read/);
set(collection + 8, objects);
vm.runInContext('fixtureIdentities[1].id = 5000010', context);
assert.throws(verify, /duplicate entity identity: .*5000010.*first_index.*second_index/);
console.log('entity collection mutation and duplicate diagnostics passed');

// A transient collection compaction must be retried by the production reader
// before it is surfaced as a capture error. The strict verifier remains the
// final gate for the frame that is published.
const retry = vm.runInContext('readWithConsistencyRetries', context);
let reads = 0;
assert.equal(retry(() => {
  reads++;
  if (reads === 1) throw new Error('entity collection changed during read: count=2->1');
  return 'stable-frame';
}, 3), 'stable-frame');
assert.equal(reads, 2);
reads = 0;
assert.throws(() => retry(() => {
  reads++;
  throw new Error('entity collection changed during read: count=2->1');
}, 3), /entity collection changed during read/);
assert.equal(reads, 3);
reads = 0;
assert.equal(retry(() => {
  reads++;
  if (reads === 1) throw new Error('duplicate entity identity: {"id":5000010}');
  return 'deduplicated-frame';
}, 3), 'deduplicated-frame');
assert.equal(reads, 2);
assert.throws(() => retry(() => {
  throw new Error('invalid HP component type');
}, 3), /invalid HP component type/);
console.log('entity collection retry guard present');
