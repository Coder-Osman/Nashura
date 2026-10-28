'use strict';

const fixturePackage = 'org.nashura.backupfixture';
const scopes = new Map();
const events = [];
const hooks = [];
let nextScope = 0;
let nextPlatformScope = 0;
let dropped = 0;
let observationErrors = 0;
let installed = false;

function record(event, details) {
  try {
    if (events.length >= 4096) { dropped++; return; }
    events.push({ event: event, details: details, thread: Process.getCurrentThreadId(),
      timeMs: Date.now() });
  } catch (error) { observationErrors++; }
}

function observe(operation) {
  try { return operation(); }
  catch (error) {
    observationErrors++;
    record('observer_error', { error: String(error), coverage: 'UNKNOWN' });
    return null;
  }
}

function reflected(object, name) {
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
  throw new Error('field absent: ' + name);
}

function journalPath(journal) {
  return journal === null ? null : String(Java.cast(reflected(journal, 'mFile'),
    Java.use('java.io.File')).getAbsolutePath());
}

function scheduled(userId) {
  const clazz = Java.use('com.android.server.backup.KeyValueBackupJob').class;
  const field = clazz.getDeclaredField('sScheduledForUserId');
  field.setAccessible(true);
  const array = field.get.overload('java.lang.Object').call(field, null);
  return Java.cast(array, Java.use('android.util.SparseBooleanArray')).get(userId);
}

function serviceState(service) {
  const pending = Java.cast(reflected(service, 'mPendingBackups'), Java.use('java.util.HashMap'));
  return { targetPending: pending.containsKey(fixturePackage), pendingCount: pending.size(),
    journalPath: journalPath(reflected(service, 'mJournal')) };
}

function fixtureState() {
  const binder = Java.use('android.os.ServiceManager').getService('backup');
  if (binder === null) throw new Error('backup service absent');
  const users = Java.cast(reflected(binder, 'mUserServices'), Java.use('android.util.SparseArray'));
  const service = users.get(0);
  if (service === null) throw new Error('user 0 backup service absent');
  return { service: serviceState(service), scheduled: scheduled(0) };
}

function installScoped(className, methodName) {
  Java.use(className)[methodName].overloads.forEach(function (method) {
    const types = method.argumentTypes.map(function (type) { return type.className; });
    const identity = className + '.' + methodName + '(' + types.join(',') + ')';
    method.implementation = function () {
      const args = Array.prototype.slice.call(arguments);
      const thread = Process.getCurrentThreadId();
      const previous = scopes.get(thread);
      if (methodName !== 'schedule' && (!previous || String(args[0]) !== fixturePackage)) {
        return method.apply(this, args);
      }
      const scope = previous || { id: 'platform-' + (++nextPlatformScope),
        origin: 'platform_schedule_without_fixture_scope' };
      if (!previous) {
        scopes.set(thread, scope);
        record('platform_scope', { scope: scope.id, origin: scope.origin,
          stack: observe(function () {
            return Java.use('java.lang.Thread').currentThread().getStackTrace()
              .map(function (frame) { return String(frame); }).slice(0, 16);
          }) });
      }
      const before = observe(function () {
        if (methodName === 'schedule') return { scheduled: scheduled(args[0]), userId: args[0] };
        if (methodName === 'addPackage') return { journalPath: journalPath(this) };
        return serviceState(this);
      }.bind(this));
      record('callee_enter', { scope: scope.id, method: identity, state: before });
      try {
        const result = method.apply(this, args);
        const after = observe(function () {
          if (methodName === 'schedule') return { scheduled: scheduled(args[0]), userId: args[0] };
          if (methodName === 'addPackage') return { journalPath: journalPath(this) };
          return serviceState(this);
        }.bind(this));
        record('callee_return', { scope: scope.id, method: identity, state: after });
        return result;
      } catch (error) {
        record('callee_throw', { scope: scope.id, method: identity, error: String(error) });
        throw error;
      } finally {
        if (!previous) scopes.delete(thread);
      }
    };
    hooks.push(identity);
  });
}

rpc.exports = {
  inspect: function () {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try { resolve(fixtureState()); } catch (error) { reject(new Error(String(error))); }
      });
    });
  },
  install: function () {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          if (installed || Java.use('android.os.Process').myUid() !== 1000 ||
              Java.use('android.app.ActivityThread').currentProcessName() !== null) {
            throw new Error('new system_server attachment required');
          }
          const service = Java.use('com.android.server.backup.UserBackupManagerService');
          const request = service.dataChanged.overload('java.lang.String');
          request.implementation = function (packageName) {
            if (String(packageName) === fixturePackage) {
              record('binder_request', { packageName: fixturePackage,
                callingUid: observe(function () { return Java.use('android.os.Binder').getCallingUid(); }) });
            }
            return request.call(this, packageName);
          };
          hooks.push('com.android.server.backup.UserBackupManagerService.dataChanged(java.lang.String)');
          const identityMap = Java.use('java.util.IdentityHashMap');
          const put = Java.use('java.util.HashMap').put.overload('java.lang.Object', 'java.lang.Object');
          put.implementation = function (key, value) {
            const scope = scopes.get(Process.getCurrentThreadId());
            const matches = scope && observe(function () {
              return String(key) === fixturePackage && scope.queues && scope.queues.containsKey(this);
            }.bind(this));
            const result = put.call(this, key, value);
            if (matches) record('queue_put_return', { scope: scope.id, previousNull: result === null });
            return result;
          };
          hooks.push('java.util.HashMap.put(java.lang.Object,java.lang.Object)');
          const entry = service.dataChangedImpl.overload('java.lang.String', 'java.util.HashSet');
          entry.implementation = function (packageName, targets) {
            if (String(packageName) !== fixturePackage) return entry.call(this, packageName, targets);
            const thread = Process.getCurrentThreadId();
            const previous = scopes.get(thread);
            const scope = observe(function () {
              const queues = identityMap.$new();
              queues.put(reflected(this, 'mPendingBackups'), Java.use('java.lang.String').$new('pending'));
              return { id: String(++nextScope), queues: queues };
            }.bind(this));
            if (!scope) return entry.call(this, packageName, targets);
            scopes.set(thread, scope);
            const before = observe(function () { return serviceState(this); }.bind(this));
            record('entry', { scope: scope.id, targetsNull: targets === null,
              targetMember: observe(function () { return targets === null ? null : targets.contains(packageName); }),
              state: before });
            try {
              const result = entry.call(this, packageName, targets);
              record('return', { scope: scope.id,
                state: observe(function () { return serviceState(this); }.bind(this)) });
              return result;
            } catch (error) {
              record('throw', { scope: scope.id, error: String(error) });
              throw error;
            } finally {
              if (previous) scopes.set(thread, previous); else scopes.delete(thread);
            }
          };
          hooks.push('com.android.server.backup.UserBackupManagerService.dataChangedImpl(java.lang.String,java.util.HashSet)');
          installScoped('com.android.server.backup.UserBackupManagerService', 'writeToJournalLocked');
          installScoped('com.android.server.backup.DataChangedJournal', 'addPackage');
          installScoped('com.android.server.backup.KeyValueBackupJob', 'schedule');
          const submit = Java.use('android.app.JobSchedulerImpl').schedule.overload('android.app.job.JobInfo');
          submit.implementation = function (job) {
            const scope = scopes.get(Process.getCurrentThreadId());
            if (!scope) return submit.call(this, job);
            const details = observe(function () {
              return { scope: scope.id, id: job.getId(), service: String(job.getService()),
                minLatency: Number(job.getMinLatencyMillis()), maxDelay: Number(job.getMaxExecutionDelayMillis()),
                network: String(job.getRequiredNetwork()), charging: job.isRequireCharging(),
                extras: String(job.getExtras()) };
            });
            record('job_submit', details);
            try {
              const result = submit.call(this, job);
              record('job_submit_return', { scope: scope.id, result: result });
              return result;
            } catch (error) {
              record('job_submit_throw', { scope: scope.id, error: String(error) });
              throw error;
            }
          };
          hooks.push('android.app.JobSchedulerImpl.schedule(android.app.job.JobInfo)');
          installed = true;
          resolve({ schema: 'nashura.backup-fixture-observer.v1', pid: Process.id,
            installed: true, hooks: hooks.slice(), targetPackage: fixturePackage });
        } catch (error) { reject(new Error(String(error))); }
      });
    });
  },
  snapshot: function () {
    return { schema: 'nashura.backup-fixture-observer.v1', installed: installed, pid: Process.id,
      events: events.slice(), dropped: dropped, observationErrors: observationErrors,
      activeScopes: scopes.size, entries: nextScope, platformScopes: nextPlatformScope,
      catalogReadyPass: false };
  }
};
