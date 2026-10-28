'use strict';

const targetPackage = 'nullsroyale.rel.free';
const activePackageThreads = new Map();
let nextScopeId = 0;
let pendingDiskCommits = null;

function safeValue(value) {
  if (value === null || value === undefined) return value;
  const kind = typeof value;
  if (kind === 'string' || kind === 'number' || kind === 'boolean') return value;
  try {
    if (value.$className === 'java.io.File' || value.getClass().getName().toString() === 'java.io.File') {
      return { className: 'java.io.File', path: String(value.getPath()) };
    }
  } catch (_) {
  }
  try {
    return { className: value.$className || value.getClass().getName().toString() };
  } catch (_) {
    return { kind: kind };
  }
}

function emit(event, details) {
  send({
    event: event,
    pid: Process.id,
    threadId: Process.getCurrentThreadId(),
    timeMs: Date.now(),
    details: details || {}
  });
}

function stack() {
  try {
    return Java.backtrace({ limit: 14 }).frames.map(function (frame) {
      return frame.signature;
    });
  } catch (error) {
    return ['<stack-unavailable:' + error + '>'];
  }
}

function observe(phase, operation) {
  try {
    return operation();
  } catch (error) {
    emit('observer_error', { phase: phase, error: String(error), coverage: 'UNKNOWN' });
    return null;
  }
}

function hookMethods(className, methodName, filter, trackPackageScope, observeAll) {
  let target;
  try {
    target = Java.use(className);
  } catch (error) {
    emit('hook_unavailable', { className: className, methodName: methodName,
      reason: String(error) });
    return;
  }

  let methods;
  try {
    methods = target[methodName].overloads;
  } catch (error) {
    emit('method_unavailable', { className: className, methodName: methodName,
      reason: String(error) });
    return;
  }

  methods.forEach(function (method, overloadIndex) {
    const signature = method.argumentTypes.map(function (type) {
      return type.className;
    });
    method.implementation = function () {
      const args = Array.prototype.slice.call(arguments);
      const values = args.map(safeValue);
      const threadId = Process.getCurrentThreadId();
      const identity = className + '.' + methodName + '(' + signature.join(',') + ')';
      const matches = filter ? filter(values) : false;
      const previousScope = activePackageThreads.get(threadId);
      let scope = previousScope;
      const diskOperation = className === 'android.app.SharedPreferencesImpl' &&
        (methodName === 'enqueueDiskWrite' || methodName === 'writeToFile');
      if (!scope && diskOperation && args[0] && pendingDiskCommits !== null) {
        const scopeId = observe('lookup_disk_commit', function () {
          return pendingDiskCommits.get(args[0]);
        });
        if (scopeId !== null) scope = { scopeId: String(scopeId),
          attribution: 'memory_commit_result_identity' };
      }
      if (!scope && trackPackageScope && (matches || values.indexOf(targetPackage) !== -1)) {
        scope = { scopeId: String(++nextScopeId), attribution: 'matched_entry',
          origin: identity };
      }
      if (!observeAll && !matches && !scope) {
        if (methodName === 'writeToFile' && diskOperation) {
          emit('unattributed_disk_write', { method: identity,
            coverage: 'UNTRACKED_MEMORY_COMMIT_RESULT' });
        }
        return method.apply(this, args);
      }
      if (scope) activePackageThreads.set(threadId, scope);

      emit('call_enter', { method: identity, overloadIndex: overloadIndex,
        args: values, packageScope: Boolean(scope), scope: scope || null,
        observeAll: observeAll === true,
        receiver: safeValue(this), stack: stack() });
      try {
        const result = method.apply(this, args);
        if (scope && methodName === 'commitToMemory' && result && pendingDiskCommits !== null) {
          observe('link_disk_commit', function () {
            if (pendingDiskCommits.size() < 256) {
              pendingDiskCommits.put(result, Java.use('java.lang.String').$new(scope.scopeId));
              emit('disk_commit_linked', { scope: scope,
                association: 'java.util.IdentityHashMap' });
            } else {
              emit('disk_commit_untracked', { scope: scope, reason: 'association_limit' });
            }
          });
        }
        emit('call_return', { method: identity, result: safeValue(result),
          packageScope: Boolean(scope), scope: scope || null,
          observeAll: observeAll === true });
        return result;
      } catch (error) {
        emit('call_throw', { method: identity, error: String(error),
          packageScope: Boolean(scope), scope: scope || null,
          observeAll: observeAll === true });
        throw error;
      } finally {
        if (scope && diskOperation && methodName === 'writeToFile' && args[0] &&
            pendingDiskCommits !== null) {
          observe('release_disk_commit', function () {
            pendingDiskCommits.remove(args[0]);
            emit('disk_commit_released', { scope: scope });
          });
        }
        if (previousScope) activePackageThreads.set(threadId, previousScope);
        else activePackageThreads.delete(threadId);
      }
    };
    emit('hook_installed', { className: className, methodName: methodName,
      overloadIndex: overloadIndex, arguments: signature });
  });
}

Java.perform(function () {
  const processName = Java.use('android.app.ActivityThread').currentProcessName();
  const processNameText = processName === null ? null : String(processName);
  const uid = Java.use('android.os.Process').myUid();
  emit('observer_ready', { processName: processNameText, uid: uid });

  if (uid === 1000 && processNameText === null) {
    hookMethods('com.android.server.backup.BackupManagerService', 'dataChanged', function (values) {
      return values.indexOf(targetPackage) !== -1;
    }, false);
    hookMethods('com.android.server.backup.BackupManagerService', 'dataChangedForUser', function (values) {
      return values.indexOf(targetPackage) !== -1;
    }, false);
    hookMethods('com.android.server.backup.BackupManagerService',
      'getServiceForUserIfCallerHasPermission', function (values) {
        return values.indexOf(targetPackage) !== -1;
      }, false);
    hookMethods('com.android.server.backup.BackupManagerService', 'isUserReadyForBackup', function () {
      return true;
    }, false);
    hookMethods('com.android.server.backup.UserBackupManagerService', 'dataChanged', function (values) {
      return values.indexOf(targetPackage) !== -1;
    }, false);
    hookMethods('com.android.server.backup.UserBackupManagerService', 'dataChangedImpl', function (values) {
      return values.indexOf(targetPackage) !== -1;
    }, false);
    hookMethods('com.android.server.backup.UserBackupManagerService', 'dataChangedTargets', function (values) {
      return values.indexOf(targetPackage) !== -1;
    }, false);
    hookMethods('com.android.server.backup.UserBackupManagerService', 'writeToJournalLocked', function (values) {
      return values.indexOf(targetPackage) !== -1;
    }, false);
    hookMethods('com.android.server.backup.DataChangedJournal', 'addPackage', function (values) {
      return values.indexOf(targetPackage) !== -1;
    }, false);
    hookMethods('com.android.server.backup.KeyValueBackupJob', 'schedule', null, false, true);
  } else {
    pendingDiskCommits = Java.use('java.util.IdentityHashMap').$new();
    hookMethods('com.supercell.titan.GameApp', 'requestBackup', function () {
      return true;
    }, true);
    hookMethods('com.supercell.titan.ApplicationUtilBase', 'storeKeyValue', function (values) {
      return values.indexOf('LOADING_SCREEN_STATE_LABEL') !== -1;
    }, true);
    hookMethods('com.supercell.titan.ApplicationUtilBase', 'getKeyValue', function (values) {
      return values.indexOf('LOADING_SCREEN_STATE_LABEL') !== -1;
    }, true);
    hookMethods('c.a', 'j', null, true);
    hookMethods('c.a', 'n', null, true);
    hookMethods('android.app.SharedPreferencesImpl$EditorImpl', 'putString', function (values) {
      return values.indexOf('LOADING_SCREEN_STATE_LABEL') !== -1;
    }, true);
    hookMethods('android.app.SharedPreferencesImpl$EditorImpl', 'remove', function (values) {
      return values.indexOf('LOADING_SCREEN_STATE_LABEL') !== -1;
    }, true);
    hookMethods('android.app.SharedPreferencesImpl$EditorImpl', 'apply', null, true);
    hookMethods('android.app.SharedPreferencesImpl$EditorImpl', 'commitToMemory', null, true);
    hookMethods('android.app.SharedPreferencesImpl$EditorImpl', 'commit', null, true);
    hookMethods('android.app.SharedPreferencesImpl', 'edit', null, true);
    hookMethods('android.app.SharedPreferencesImpl', 'contains', function (values) {
      return values.indexOf('LOADING_SCREEN_STATE_LABEL') !== -1;
    }, true);
    hookMethods('android.app.SharedPreferencesImpl', 'getString', function (values) {
      return values.indexOf('LOADING_SCREEN_STATE_LABEL') !== -1;
    }, true);
    hookMethods('android.app.SharedPreferencesImpl', 'enqueueDiskWrite', null, true);
    hookMethods('android.app.SharedPreferencesImpl', 'writeToFile', null, true);
    hookMethods('android.app.SharedPreferencesImpl', 'loadFromDisk', null, true);
    hookMethods('android.app.SharedPreferencesImpl', 'createFileOutputStream', null, true);
    hookMethods('android.app.backup.BackupManager', 'dataChanged', function (values) {
      return values.indexOf(targetPackage) !== -1;
    }, false);
    hookMethods('android.app.backup.IBackupManager$Stub$Proxy', 'dataChanged', function (values) {
      return values.indexOf(targetPackage) !== -1;
    }, false);
  }
});

rpc.exports = {
  triggerbackup: function () {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          const gameApp = Java.use('com.supercell.titan.GameApp');
          const instance = gameApp.getInstance();
          emit('manual_requestBackup_begin', {
            method: 'com.supercell.titan.GameApp.requestBackup:()V',
            invocation: 'instance',
            receiverClass: instance.getClass().getName().toString()
          });
          instance.requestBackup();
          emit('manual_requestBackup_return', {
            method: 'com.supercell.titan.GameApp.requestBackup:()V',
            invocation: 'instance'
          });
          resolve({ invoked: true, invocation: 'instance' });
        } catch (error) {
          emit('manual_requestBackup_error', { error: String(error) });
          reject(String(error));
        }
      });
    });
  },
  triggerstore: function (key, value) {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          const applicationUtil = Java.use('com.supercell.titan.ApplicationUtilBase');
          const keyText = String(key);
          const valueText = String(value);
          emit('manual_storeKeyValue_begin', { key: keyText, value: valueText,
            method: 'com.supercell.titan.ApplicationUtilBase.storeKeyValue' });
          applicationUtil.storeKeyValue(keyText, valueText);
          emit('manual_storeKeyValue_return', { key: keyText, value: valueText,
            method: 'com.supercell.titan.ApplicationUtilBase.storeKeyValue' });
          resolve({ invoked: true, key: keyText, value: valueText });
        } catch (error) {
          emit('manual_storeKeyValue_error', { error: String(error), key: String(key) });
          reject(String(error));
        }
      });
    });
  },
  readkey: function (key) {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          const applicationUtil = Java.use('com.supercell.titan.ApplicationUtilBase');
          const keyText = String(key);
          const value = applicationUtil.getKeyValue(keyText);
          const valueText = value === null || value === undefined ? null : String(value);
          emit('manual_getKeyValue_return', { key: keyText, value: valueText });
          resolve({ key: keyText, value: valueText });
        } catch (error) {
          emit('manual_getKeyValue_error', { error: String(error), key: String(key) });
          reject(String(error));
        }
      });
    });
  }
};
