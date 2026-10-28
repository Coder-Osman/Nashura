'use strict';

const deliverySchema = 'nashura.backup-delivery-observer.v1';
const deliveryPackage = 'org.nashura.backupfixture';
const deliveryEvents = [];
const deliveryHooks = [];
const deliveryScopes = new Map();
let deliveryInstalled = false;
let deliveryDropped = 0;
let deliveryErrors = 0;
let deliverySequence = 0;

function deliveryRecord(event, details) {
  try {
    if (deliveryEvents.length >= 4096) { deliveryDropped++; return; }
    deliveryEvents.push({ sequence: deliveryEvents.length, event: event,
      thread: Process.getCurrentThreadId(), monotonicMs: Number(Java.use('android.os.SystemClock').elapsedRealtime()),
      details: details });
    send({ kind: 'delivery_event', value: deliveryEvents[deliveryEvents.length - 1] });
  } catch (error) { deliveryErrors++; }
}

function deliveryObserve(operation) {
  try { return operation(); } catch (error) {
    deliveryErrors++;
    deliveryRecord('observer_error', { error: String(error), coverage: 'UNKNOWN' });
    return null;
  }
}

function deliveryField(object, name) {
  let clazz = Java.cast(object, Java.use('java.lang.Object')).getClass();
  while (clazz !== null) {
    try {
      const field = clazz.getDeclaredField(name);
      field.setAccessible(true);
      return field.get.overload('java.lang.Object').call(field, object);
    } catch (error) {
      if (String(error).indexOf('NoSuchFieldException') < 0) throw error;
      clazz = clazz.getSuperclass();
    }
  }
  throw new Error('missing field: ' + name);
}

function deliveryScheduled() {
  const clazz = Java.use('com.android.server.backup.KeyValueBackupJob').class;
  const field = clazz.getDeclaredField('sScheduledForUserId');
  field.setAccessible(true);
  return Java.cast(field.get.overload('java.lang.Object').call(field, null),
    Java.use('android.util.SparseBooleanArray')).get(0);
}

function deliveryInspect() {
  const binder = Java.use('android.os.ServiceManager').getService('backup');
  if (binder === null) throw new Error('backup service absent');
  const users = Java.cast(deliveryField(binder, 'mUserServices'), Java.use('android.util.SparseArray'));
  const service = users.get(0);
  if (service === null) throw new Error('backup user absent');
  const pending = Java.cast(deliveryField(service, 'mPendingBackups'), Java.use('java.util.HashMap'));
  const scheduler = Java.cast(deliveryField(service, 'mContext'), Java.use('android.content.Context'))
    .getSystemService('jobscheduler');
  const jobs = Java.cast(scheduler, Java.use('android.app.job.JobScheduler')).getAllPendingJobs();
  const selected = [];
  for (let index = 0; index < jobs.size(); index++) {
    const job = Java.cast(jobs.get(index), Java.use('android.app.job.JobInfo'));
    if (String(job.getService().getClassName()) === 'com.android.server.backup.KeyValueBackupJob') {
      selected.push({ id: job.getId(), component: String(job.getService()),
        userId: job.getTransientExtras().getInt('userId'), minLatency: Number(job.getMinLatencyMillis()) });
    }
  }
  return { pending: pending.containsKey(deliveryPackage), scheduled: deliveryScheduled(), jobs: selected };
}

function deliveryHook(className, methodName, argumentsTypes, select, details) {
  const method = Java.use(className)[methodName].overload.apply(Java.use(className)[methodName], argumentsTypes);
  const identity = className + '.' + methodName + '(' + argumentsTypes.join(',') + ')';
  method.implementation = function () {
    const args = Array.prototype.slice.call(arguments);
    if (select && !deliveryObserve(function () { return select(this, args); }.bind(this))) {
      return method.apply(this, args);
    }
    const thread = Process.getCurrentThreadId();
    const previous = deliveryScopes.get(thread);
    const call = String(++deliverySequence);
    deliveryScopes.set(thread, call);
    const before = deliveryObserve(function () { return details(this, args, null, false); }.bind(this));
    deliveryRecord('enter', { call: call, parent: previous || null, method: identity, value: before });
    try {
      const result = method.apply(this, args);
      deliveryRecord('return', { call: call, method: identity,
        value: deliveryObserve(function () { return details(this, args, result, true); }.bind(this)) });
      return result;
    } catch (error) {
      deliveryRecord('throw', { call: call, method: identity, error: String(error) });
      throw error;
    } finally {
      if (previous) deliveryScopes.set(thread, previous); else deliveryScopes.delete(thread);
    }
  };
  deliveryHooks.push(identity);
}

rpc.exports = {
  install: function () {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          if (deliveryInstalled || Java.use('android.os.Process').myUid() !== 1000 ||
              Java.use('android.app.ActivityThread').currentProcessName() !== null) {
            throw new Error('fresh system_server attachment required');
          }
          const job = 'com.android.server.backup.KeyValueBackupJob';
          deliveryHook(job, 'onStartJob', ['android.app.job.JobParameters'], null,
            function (object, args, result, returned) {
              return { id: args[0].getJobId(), userId: args[0].getTransientExtras().getInt('userId'),
                scheduled: deliveryScheduled(), result: returned ? result : null };
            });
          deliveryHook(job, 'clearScheduledForUserId', ['int'], null,
            function (object, args) { return { userId: args[0], scheduled: deliveryScheduled() }; });
          deliveryHook(job, 'cancel', ['int', 'android.content.Context'], null,
            function (object, args) { return { userId: args[0], scheduled: deliveryScheduled() }; });
          deliveryHook('com.android.server.backup.UserBackupManagerService', 'backupNow', [], null,
            function () { return deliveryInspect(); });
          deliveryInstalled = true;
          resolve({ schema: deliverySchema, installed: true, pid: Process.id, hooks: deliveryHooks.slice(),
            targetPackage: deliveryPackage, workerCoverage: 'UNINSTRUMENTED_AFTER_OBSERVER_CRASH' });
        } catch (error) { reject(new Error(String(error))); }
      });
    });
  },
  inspect: function () {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try { resolve(deliveryInspect()); } catch (error) { reject(new Error(String(error))); }
      });
    });
  },
  snapshot: function () {
    return { schema: deliverySchema, installed: deliveryInstalled, pid: Process.id,
      events: deliveryEvents.slice(), dropped: deliveryDropped, observationErrors: deliveryErrors,
      activeScopes: deliveryScopes.size, catalogReadyPass: false };
  }
};
