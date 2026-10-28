'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const classes = new Map();
const bodies = new Map();
const counts = new Map();
const rpc = { exports: {} };
let thread = 1;
let clock = 0;
let scheduled = true;
let observeFailure = false;

function clazz(name) {
  if (!classes.has(name)) classes.set(name, new Proxy({}, {
    get(object, member) {
      if (!(member in object)) {
        const overloads = new Map();
        object[member] = { overload(...types) {
          const signature = name + '.' + String(member) + '(' + types.join(',') + ')';
          if (!overloads.has(signature)) {
            const method = { signature, apply(receiver, args) {
              counts.set(signature, (counts.get(signature) || 0) + 1);
              return bodies.get(signature)?.apply(receiver, args);
            } };
            overloads.set(signature, method);
          }
          return overloads.get(signature);
        } };
      }
      return object[member];
    }
  }));
  return classes.get(name);
}

const jobClass = 'com.android.server.backup.KeyValueBackupJob';
const backup = clazz('com.android.server.backup.UserBackupManagerService').backupNow.overload();
const start = clazz(jobClass).onStartJob.overload('android.app.job.JobParameters');
const cancel = clazz(jobClass).cancel.overload('int', 'android.content.Context');
clazz(jobClass).class = { getDeclaredField() {
  return { setAccessible() {}, get: { overload() { return { call() {
    if (observeFailure) throw new Error('observe');
    return { get: () => scheduled };
  } }; } } };
} };
const service = { mPendingBackups: { containsKey: () => true },
  mContext: { getSystemService: () => ({ getAllPendingJobs: () => ({ size: () => 0 }) }) } };
const binder = { mUserServices: { get: () => service } };
for (const instance of [service, binder]) instance.getClass = () => ({ getDeclaredField(name) {
  return { setAccessible() {}, get: { overload() { return { call(field, object) { return object[name]; } }; } } };
} });
const context = {
  rpc, send() {}, Process: { id: 522, getCurrentThreadId: () => thread },
  Java: { perform: body => body(), cast: object => object, use(name) {
    if (name === 'android.os.Process') return { myUid: () => 1000 };
    if (name === 'android.app.ActivityThread') return { currentProcessName: () => null };
    if (name === 'android.os.SystemClock') return { elapsedRealtime: () => ++clock };
    if (name === 'android.os.ServiceManager') return { getService: () => binder };
    return clazz(name);
  } }
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const parameters = { getJobId: () => 42, getTransientExtras: () => ({ getInt: () => 0 }) };
bodies.set(cancel.signature, () => { scheduled = false; });
bodies.set(backup.signature, () => cancel.implementation.call({}, 0, {}));
bodies.set(start.signature, () => { scheduled = false; backup.implementation.call({}); return false; });

const scenarios = {
  nested() {
    assert.equal(start.implementation.call({}, parameters), false);
    for (const method of [start, backup, cancel]) assert.equal(counts.get(method.signature), 1);
    const events = rpc.exports.snapshot().events;
    assert.equal(events[1].details.parent, events[0].details.call);
    assert.equal(events[2].details.parent, events[1].details.call);
    assert.equal(events[5].details.value.result, false);
  },
  exception() {
    const error = new Error('original');
    bodies.set(start.signature, () => { throw error; });
    assert.throws(() => start.implementation.call({}, parameters), value => value === error);
    assert.equal(counts.get(start.signature), 1);
    assert.equal(rpc.exports.snapshot().events[1].event, 'throw');
  },
  interleaved() {
    bodies.set(start.signature, () => {
      thread = 2;
      cancel.implementation.call({}, 0, {});
      thread = 1;
      return false;
    });
    start.implementation.call({}, parameters);
    const events = rpc.exports.snapshot().events;
    assert.equal(events[1].details.parent, null);
    assert.equal(events[1].thread, 2);
    assert.equal(counts.get(start.signature), 1);
    assert.equal(counts.get(cancel.signature), 1);
  },
  observer_failure() {
    observeFailure = true;
    assert.equal(start.implementation.call({}, parameters), false);
    for (const method of [start, backup, cancel]) assert.equal(counts.get(method.signature), 1);
    assert.ok(rpc.exports.snapshot().observationErrors > 0);
  },
  limit() {
    for (let index = 0; index < 2050; index++) cancel.implementation.call({}, 0, {});
    const observed = rpc.exports.snapshot();
    assert.equal(observed.events.length, 4096);
    assert.equal(observed.dropped, 4);
    assert.equal(counts.get(cancel.signature), 2050);
  }
};

(async function () {
  const readiness = await rpc.exports.install();
  assert.equal(readiness.hooks.length, 4);
  assert.equal(readiness.workerCoverage, 'UNINSTRUMENTED_AFTER_OBSERVER_CRASH');
  scenarios[process.argv[3]]();
  assert.equal(rpc.exports.snapshot().activeScopes, 0);
  assert.equal(rpc.exports.snapshot().catalogReadyPass, false);
  process.stdout.write('MOCK_DELIVERY_PASS ' + process.argv[3] + '\n');
})().catch(error => { process.stderr.write(String(error.stack) + '\n'); process.exitCode = 1; });
