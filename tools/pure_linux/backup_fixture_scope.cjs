'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const classes = new Map();
const bodies = new Map();
const counts = new Map();
const rpc = { exports: {} };
const prefix = 'com.android.server.backup.';
const service = prefix + 'UserBackupManagerService';
const target = 'org.nashura.backupfixture';
let scheduled = false;
let observeFailure = false;
let thread = 1;

function clazz(name) {
  if (!classes.has(name)) classes.set(name, new Proxy({}, {
    get(object, member) {
      if (!(member in object)) {
        const overloads = [];
        object[member] = { overloads, overload(...types) {
          const signature = name + '.' + String(member) + '(' + types.join(',') + ')';
          let method = overloads.find(value => value.signature === signature);
          if (!method) {
            method = { signature, argumentTypes: types.map(className => ({ className })),
              call(receiver, ...args) {
                counts.set(signature, (counts.get(signature) || 0) + 1);
                const body = bodies.get(signature);
                return body ? body.apply(receiver, args) : undefined;
              }, apply(receiver, args) { return method.call(receiver, ...args); } };
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
const write = clazz(service).writeToJournalLocked.overload('java.lang.String');
const append = clazz(prefix + 'DataChangedJournal').addPackage.overload('java.lang.String');
const short = clazz(prefix + 'KeyValueBackupJob').schedule.overload('int', 'android.content.Context', prefix + 'BackupManagerConstants');
const long = clazz(prefix + 'KeyValueBackupJob').schedule.overload('int', 'android.content.Context', 'long', prefix + 'BackupManagerConstants');
const submit = clazz('android.app.JobSchedulerImpl').schedule.overload('android.app.job.JobInfo');
const instance = { mPendingBackups: new Map(), mJournal: { mFile: { getAbsolutePath: () => '/fixture/journal' } } };
const job = { getId: () => 1, getService: () => 'fixture', getMinLatencyMillis: () => 10,
  getMaxExecutionDelayMillis: () => 86400000, getRequiredNetwork: () => 'fixture',
  isRequireCharging: () => true, getExtras: () => 'fixture' };

function field(name) {
  return { setAccessible() {}, get: { overload() { return { call(object, receiver) {
    return name === 'sScheduledForUserId' ? { get: () => scheduled } : receiver[name];
  } }; } } };
}
clazz(prefix + 'KeyValueBackupJob').class = { getDeclaredField: field };
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  rpc, Date, Process: { id: 522, getCurrentThreadId: () => thread },
  Java: { perform: body => body(), cast(object) {
    return object instanceof Map ? {
      containsKey: key => object.has(key), size: () => object.size,
    } : object;
  }, use(name) {
    if (name === 'java.lang.Object') return {};
    if (name === 'android.os.Process') return { myUid: () => 1000 };
    if (name === 'android.app.ActivityThread') return { currentProcessName: () => null };
    if (name === 'java.lang.Thread') return { currentThread: () => ({ getStackTrace: () => ['fixture'] }) };
    if (name === 'android.os.Binder') return { getCallingUid: () => {
      if (observeFailure) throw new Error('observer');
      return 10001;
    } };
    if (name === 'java.lang.String') return { $new: text => text };
    if (name === 'java.util.IdentityHashMap') return { $new() {
      const values = new Map();
      return { put(key, value) { values.set(key, value); }, containsKey: key => values.has(key) };
    } };
    return clazz(name);
  } }
});
instance.getClass = () => ({ getDeclaredField: field });
instance.mJournal.getClass = () => ({ getDeclaredField: field });
bodies.set(put.signature, function (key, value) {
  const previous = this.get(key) ?? null;
  this.set(key, value);
  return previous;
});
bodies.set(short.signature, function (user, context, constants) {
  return long.implementation.call(this, user, context, 0, constants);
});
bodies.set(long.signature, function () {
  if (!scheduled) { submit.implementation.call({}, job); scheduled = true; }
  return 'schedule-return';
});
bodies.set(submit.signature, () => 1);
bodies.set(write.signature, function (key) { append.implementation.call(this.mJournal, key); });
bodies.set(entry.signature, function (key, targets) {
  if (targets && targets.has(key) && put.implementation.call(this.mPendingBackups, key, {}) === null) {
    write.implementation.call(this, key);
  }
  if (targets) long.implementation.call({}, 0, {}, 0, {});
  return 'entry-return';
});

const scenarios = {
  platform_nested() {
    assert.equal(short.implementation.call({}, 0, {}, {}), 'schedule-return');
    const observed = rpc.exports.snapshot();
    assert.equal(observed.platformScopes, 1);
    assert.equal(observed.entries, 0);
    assert.equal(observed.events.filter(event => event.event === 'job_submit_return')[0].details.result, 1);
    assert.equal(counts.get(short.signature), 1);
    assert.equal(counts.get(long.signature), 1);
    assert.equal(counts.get(submit.signature), 1);
  },
  exception() {
    const failure = new Error('original');
    bodies.set(long.signature, () => { throw failure; });
    assert.throws(() => long.implementation.call({}, 0, {}, 0, {}), error => error === failure);
    assert.equal(counts.get(long.signature), 1);
    assert.equal(rpc.exports.snapshot().events.filter(event => event.event === 'callee_throw').length, 1);
  },
  first_duplicate() {
    scheduled = true;
    const targets = new Set([target]);
    targets.contains = key => targets.has(key);
    assert.equal(entry.implementation.call(instance, target, targets), 'entry-return');
    assert.equal(entry.implementation.call(instance, target, targets), 'entry-return');
    const observed = rpc.exports.snapshot();
    assert.deepEqual(Array.from(observed.events.filter(event => event.event === 'queue_put_return'),
      event => event.details.previousNull), [true, false]);
    assert.equal(counts.get(entry.signature), 2);
    assert.equal(counts.get(write.signature), 1);
    assert.equal(counts.get(append.signature), 1);
    assert.equal(observed.platformScopes, 0);
  },
  unrelated() {
    entry.implementation.call(instance, 'other', null);
    assert.equal(rpc.exports.snapshot().entries, 0);
    assert.equal(counts.get(entry.signature), 1);
  },
  observer_failure() {
    observeFailure = true;
    const request = clazz(service).dataChanged.overload('java.lang.String');
    request.implementation.call(instance, target);
    assert.equal(counts.get(request.signature), 1);
    assert.equal(rpc.exports.snapshot().observationErrors, 1);
  },
  interleaved() {
    bodies.set(long.signature, function () {
      if (thread === 1) { thread = 2; long.implementation.call({}, 0, {}, 0, {}); thread = 1; }
      return 'original';
    });
    long.implementation.call({}, 0, {}, 0, {});
    assert.equal(rpc.exports.snapshot().platformScopes, 2);
    assert.equal(counts.get(long.signature), 2);
  }
};

(async function () {
  const readiness = await rpc.exports.install();
  assert.equal(readiness.hooks.length, 8);
  scenarios[process.argv[3]]();
  assert.equal(rpc.exports.snapshot().activeScopes, 0);
  if (process.argv[3] !== 'observer_failure') assert.equal(rpc.exports.snapshot().observationErrors, 0);
  process.stdout.write('MOCK_FIXTURE_PASS ' + process.argv[3] + '\n');
})().catch(error => { process.stderr.write(String(error.stack) + '\n'); process.exitCode = 1; });
