'use strict';

const targetPackage = 'nullsroyale.rel.free';
const activePackageThreads = new Set();

function safeValue(value) {
  if (value === null || value === undefined) return value;
  const kind = typeof value;
  if (kind === 'string' || kind === 'number' || kind === 'boolean') return value;
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

function hookMethods(className, methodName, filter, trackPackageScope) {
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
      const isEntry = trackPackageScope && values.indexOf(targetPackage) !== -1;
      if (isEntry) activePackageThreads.add(threadId);
      const inScope = activePackageThreads.has(threadId);
      if (!matches && !inScope && !isEntry) return method.apply(this, args);

      emit('call_enter', { method: identity, overloadIndex: overloadIndex,
        args: values, packageScope: inScope || isEntry || matches, stack: stack() });
      try {
        const result = method.apply(this, args);
        emit('call_return', { method: identity, result: safeValue(result),
          packageScope: inScope || isEntry || matches });
        return result;
      } catch (error) {
        emit('call_throw', { method: identity, error: String(error),
          packageScope: inScope || isEntry || matches });
        throw error;
      } finally {
        if (isEntry) activePackageThreads.delete(threadId);
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
    hookMethods('com.android.server.backup.KeyValueBackupJob', 'schedule', null, false);
  } else {
    hookMethods('com.supercell.titan.GameApp', 'requestBackup', function () {
      return true;
    }, true);
    hookMethods('com.supercell.titan.ApplicationUtilBase', 'storeKeyValue', function (values) {
      return values.indexOf('LOADING_SCREEN_STATE_LABEL') !== -1;
    }, true);
    hookMethods('android.app.SharedPreferencesImpl$EditorImpl', 'putString', function (values) {
      return values.indexOf('LOADING_SCREEN_STATE_LABEL') !== -1;
    }, true);
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
  }
};
