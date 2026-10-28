'use strict';

const targetPackage = 'nullsroyale.rel.free';
const scopes = new Map();
const events = [];
const hooks = [];
let nextScope = 0;
let dropped = 0;
let installed = false;
let observationErrors = 0;

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
  }
  return null;
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
  throw new Error('pending queue field absent');
}

function scopedHook(className, methodName) {
  Java.use(className)[methodName].overloads.forEach(function (method) {
    const signature = method.argumentTypes.map(function (type) { return type.className; });
    const identity = className + '.' + methodName + '(' + signature.join(',') + ')';
    method.implementation = function () {
      const args = Array.prototype.slice.call(arguments);
      const scope = scopes.get(Process.getCurrentThreadId());
      if (!scope) return method.apply(this, args);
      const matches = observe(function () {
        return methodName === 'schedule' || String(args[0]) === targetPackage;
      });
      if (!matches) return method.apply(this, args);
      record('callee_enter', { scope: scope.id, method: identity });
      try {
        const result = method.apply(this, args);
        record('callee_return', { scope: scope.id, method: identity });
        return result;
      } catch (error) {
        record('callee_throw', { scope: scope.id, method: identity });
        throw error;
      }
    };
    hooks.push(identity);
  });
}

rpc.exports = {
  install: function () {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          if (installed) throw new Error('already installed');
          if (Java.use('android.os.Process').myUid() !== 1000 ||
              Java.use('android.app.ActivityThread').currentProcessName() !== null) {
            throw new Error('system_server required');
          }
          const identityMap = Java.use('java.util.IdentityHashMap');
          const strings = Java.use('java.lang.String');
          const service = Java.use('com.android.server.backup.UserBackupManagerService');
          const entry = service.dataChangedImpl.overload('java.lang.String', 'java.util.HashSet');
          const put = Java.use('java.util.HashMap').put.overload('java.lang.Object', 'java.lang.Object');
          put.implementation = function (key, value) {
            const scope = scopes.get(Process.getCurrentThreadId());
            const matched = scope && observe(function () {
              return String(key) === targetPackage && scope.queues.containsKey(this);
            }.bind(this));
            if (!matched) return put.call(this, key, value);
            try {
              const result = put.call(this, key, value);
              record('queue_put_return', { scope: scope.id, previousNull: result === null,
                identity: 'java.util.IdentityHashMap', key: targetPackage });
              return result;
            } catch (error) {
              record('queue_put_throw', { scope: scope.id });
              throw error;
            }
          };
          hooks.push('java.util.HashMap.put(java.lang.Object,java.lang.Object)');
          entry.implementation = function (packageName, targets) {
            if (String(packageName) !== targetPackage) return entry.call(this, packageName, targets);
            const thread = Process.getCurrentThreadId();
            const previous = scopes.get(thread);
            const scope = observe(function () {
              const queues = identityMap.$new();
              queues.put(reflected(this, 'mPendingBackups'), strings.$new('pending'));
              return { id: String(++nextScope), queues: queues };
            }.bind(this));
            if (!scope) return entry.call(this, packageName, targets);
            scopes.set(thread, scope);
            record('entry', { scope: scope.id, targetsNull: targets === null,
              origin: 'natural_request_only', historicalBranch: 'UNRESOLVED' });
            try {
              const result = entry.call(this, packageName, targets);
              record('return', { scope: scope.id });
              return result;
            } catch (error) {
              record('throw', { scope: scope.id });
              throw error;
            } finally {
              if (previous) scopes.set(thread, previous);
              else scopes.delete(thread);
            }
          };
          hooks.push('com.android.server.backup.UserBackupManagerService.dataChangedImpl(java.lang.String,java.util.HashSet)');
          scopedHook('com.android.server.backup.UserBackupManagerService', 'writeToJournalLocked');
          scopedHook('com.android.server.backup.DataChangedJournal', 'addPackage');
          scopedHook('com.android.server.backup.KeyValueBackupJob', 'schedule');
          installed = true;
          resolve({ schema: 'nashura.backup-branch-observer.v1', pid: Process.id,
            installed: installed, hooks: hooks.slice(), passive: true });
        } catch (error) { reject(new Error(String(error))); }
      });
    });
  },
  snapshot: function () {
    return { schema: 'nashura.backup-branch-observer.v1', installed: installed, pid: Process.id,
      passive: true, observationErrors: observationErrors,
      events: events.slice(), dropped: dropped, activeScopes: scopes.size,
      naturalEntries: nextScope, catalogReadyPass: false };
  }
};
