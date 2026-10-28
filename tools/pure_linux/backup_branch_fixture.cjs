'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const classes = new Map();
const bodies = new Map();
const rpc = { exports: {} };
let threadId = 1;
let recordFails = false;
let identityFails = false;
let originalPuts = 0;
let originalEntries = 0;
const target = 'nullsroyale.rel.free';
const prefix = 'com.android.server.backup.';
const service = prefix + 'UserBackupManagerService';

function clazz(name) {
  if (!classes.has(name)) classes.set(name, new Proxy({}, {
    get: function (object, member) {
      if (!(member in object)) {
        const overloads = [];
        object[member] = { overloads: overloads, overload: function (...types) {
          const signature = name + '.' + String(member) + '(' + types.join(',') + ')';
          let method = overloads.find(function (value) { return value.signature === signature; });
          if (!method) {
            method = { signature: signature, argumentTypes: types.map(function (type) {
              return { className: type };
            }), call: function (receiver, ...args) {
              const body = bodies.get(name + '.' + String(member));
              return body ? body.apply(receiver, args) : undefined;
            }, apply: function (receiver, args) { return method.call(receiver, ...args); } };
            overloads.push(method);
          }
          return method;
        } };
      }
      return object[member];
    }
  }));
  return classes.get(name);
}

const entry = clazz(service).dataChangedImpl.overload('java.lang.String', 'java.util.HashSet');
const put = clazz('java.util.HashMap').put.overload('java.lang.Object', 'java.lang.Object');
clazz(service).writeToJournalLocked.overload('java.lang.String');
clazz(prefix + 'DataChangedJournal').addPackage.overload('java.lang.String');
clazz(prefix + 'KeyValueBackupJob').schedule.overload('int', 'android.content.Context', prefix + 'BackupManagerConstants');
clazz(prefix + 'KeyValueBackupJob').schedule.overload('int', 'android.content.Context', 'long', prefix + 'BackupManagerConstants');

function invoke(name, member, receiver, ...args) {
  return clazz(name)[member].overloads[0].implementation.apply(receiver, args);
}

const sandbox = {
  rpc: rpc, Process: { id: 522, getCurrentThreadId: function () { return threadId; } },
  Date: { now: function () { if (recordFails) throw new Error('clock observer'); return 1; } },
  Java: { perform: function (body) { body(); },
    cast: function (object) { return { getClass: function () { return {
      getDeclaredField: function (name) {
        assert.equal(name, 'mPendingBackups');
        return { setAccessible: function () {}, get: { overload: function () {
          return { call: function (field, receiver) { return receiver.pending; } };
        } } };
      }
    }; } }; },
    use: function (name) {
      if (name === 'android.os.Process') return { myUid: function () { return 1000; } };
      if (name === 'android.app.ActivityThread') return { currentProcessName: function () { return null; } };
      if (name === 'java.lang.String') return { $new: function (text) { return text; } };
      if (name === 'java.util.IdentityHashMap') return { $new: function () {
        const identity = new Map();
        return { put: function (key, value) {
          if (identityFails) throw new Error('identity observer');
          identity.set(key, value);
        }, containsKey: function (key) { return identity.has(key); } };
      } };
      return clazz(name);
    }
  }
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);

const instance = { pending: new Map() };
bodies.set('java.util.HashMap.put', function (key, value) {
  originalPuts++;
  const previous = this.get(key) ?? null;
  this.set(key, value);
  return previous;
});
bodies.set(service + '.writeToJournalLocked', function (key) {
  invoke(prefix + 'DataChangedJournal', 'addPackage', {}, key);
});
bodies.set(service + '.dataChangedImpl', function (key, targets) {
  originalEntries++;
  if (targets === null) return 'original';
  if (targets.has(key) && put.implementation.call(this.pending, key, {}) === null) {
    invoke(service, 'writeToJournalLocked', this, key);
  }
  invoke(prefix + 'KeyValueBackupJob', 'schedule', {}, 0, {}, {});
  return 'original';
});

function enter(receiver = instance, key = target, targets = new Set([target])) {
  return entry.implementation.call(receiver, key, targets);
}
function snapshot() { return rpc.exports.snapshot(); }
function matching(event) { return Array.from(snapshot().events).filter(function (row) { return row.event === event; }); }

const cases = {
  first_duplicate: function () {
    assert.equal(enter(), 'original');
    assert.equal(enter(), 'original');
    assert.deepEqual(matching('queue_put_return').map(function (row) { return row.details.previousNull; }), [true, false]);
    assert.equal(matching('callee_enter').filter(function (row) { return row.details.method.includes('writeToJournalLocked'); }).length, 1);
    assert.equal(originalPuts, 2);
    assert.equal(snapshot().naturalEntries, 2);
  },
  unrelated_identity: function () {
    bodies.set(service + '.dataChangedImpl', function () {
      put.implementation.call(new Map(), target, {});
      put.implementation.call(this.pending, 'unrelated', {});
      threadId = 2;
      put.implementation.call(this.pending, target, {});
      threadId = 1;
      return 'original';
    });
    assert.equal(enter(), 'original');
    assert.equal(originalPuts, 3);
    assert.equal(matching('queue_put_return').length, 0);
    enter(instance, 'unrelated');
    assert.equal(snapshot().naturalEntries, 1);
  },
  null_nonmember: function () {
    enter(instance, target, null);
    enter(instance, target, new Set());
    assert.equal(originalPuts, 0);
    assert.equal(matching('callee_enter').length, 1);
    assert.ok(matching('callee_enter')[0].details.method.includes('schedule'));
  },
  nested: function () {
    let nested = false;
    const inner = { pending: new Map() };
    bodies.set(service + '.dataChangedImpl', function () {
      if (!nested) { nested = true; enter(inner); }
      put.implementation.call(this.pending, target, {});
    });
    enter();
    assert.deepEqual(matching('queue_put_return').map(function (row) { return row.details.scope; }), ['2', '1']);
    assert.equal(snapshot().activeScopes, 0);
  },
  exception: function () {
    const sentinel = new Error('original failure');
    bodies.set('java.util.HashMap.put', function () { originalPuts++; throw sentinel; });
    assert.throws(function () { enter(); }, function (error) { return error === sentinel; });
    assert.equal(originalPuts, 1);
    assert.equal(matching('queue_put_throw').length, 1);
    assert.equal(matching('throw').length, 1);
    assert.equal(snapshot().activeScopes, 0);
  },
  observer_failure: function () {
    identityFails = true;
    assert.equal(enter(), 'original');
    assert.equal(originalEntries, 1);
    assert.equal(originalPuts, 1);
    assert.equal(snapshot().observationErrors, 1);
    assert.equal(matching('observer_error').length, 1);
    assert.equal(snapshot().activeScopes, 0);
  },
  record_failure: function () {
    recordFails = true;
    assert.equal(enter(), 'original');
    assert.equal(originalPuts, 1);
    assert.ok(snapshot().observationErrors > 0);
    assert.equal(snapshot().activeScopes, 0);
  },
  bounded: function () {
    for (let index = 0; index < 1200; index++) enter();
    assert.equal(snapshot().events.length, 4096);
    assert.ok(snapshot().dropped > 0);
    assert.equal(originalPuts, 1200);
  }
};
(async function () {
  const readiness = await rpc.exports.install();
  assert.equal(readiness.hooks.length, 6);
  assert.equal(readiness.passive, true);
  assert.ok(process.argv[3] in cases);
  cases[process.argv[3]]();
  process.stdout.write('MOCK_BRANCH_PASS ' + process.argv[3] + '\n');
})().catch(function (error) { process.stderr.write(String(error.stack) + '\n'); process.exitCode = 1; });
