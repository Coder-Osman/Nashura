'use strict';

function className(object) {
  return object === null ? null : String(Java.cast(object, Java.use('java.lang.Object')).getClass().getName());
}

function field(object, name) {
  let clazz = Java.cast(object, Java.use('java.lang.Object')).getClass();
  while (clazz !== null) {
    try {
      const declared = clazz.getDeclaredField(name);
      declared.setAccessible(true);
      return declared.get.overload('java.lang.Object').call(declared, object);
    } catch (error) {
      if (String(error).indexOf('NoSuchFieldException') < 0) throw error;
      clazz = clazz.getSuperclass();
    }
  }
  throw new Error('field unavailable: ' + name);
}

rpc.exports = {
  inspect: function () {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          const uid = Java.use('android.os.Process').myUid();
          const process = Java.use('android.app.ActivityThread').currentProcessName();
          if (uid !== 1000 || process !== null) throw new Error('system_server required');
          const binder = Java.use('android.os.ServiceManager').getService('backup');
          if (binder === null) throw new Error('backup binder absent');
          if (className(binder) !== 'com.android.server.backup.BackupManagerService') {
            throw new Error('unexpected backup binder class');
          }
          const service = binder;
          const users = Java.cast(field(service, 'mUserServices'), Java.use('android.util.SparseArray'));
          const user = users.get(0);
          if (className(user) !== 'com.android.server.backup.UserBackupManagerService') {
            throw new Error('user 0 service absent or unexpected');
          }
          const target = Java.use('java.lang.String').$new('nullsroyale.rel.free');
          const context = Java.cast(field(user, 'mContext'), Java.use('android.content.Context'));
          const app = context.getPackageManager().getApplicationInfo(target, 0);
          const targetUid = app.uid.value;
          const pending = Java.cast(field(user, 'mPendingBackups'), Java.use('java.util.HashMap'));
          const participants = Java.cast(field(user, 'mBackupParticipants'), Java.use('android.util.SparseArray'));
          function sample() {
            const targets = participants.get(targetUid);
            const set = targets === null ? null : Java.cast(targets, Java.use('java.util.HashSet'));
            const journal = field(user, 'mJournal');
            return {
              pending_count: pending.size(), target_pending: pending.containsKey(target),
              participant_uid_count: participants.size(), target_set_present: set !== null,
              target_set_count: set === null ? null : set.size(),
              target_participates: set === null ? null : set.contains(target),
              journal_present: journal !== null, journal_class: className(journal)
            };
          }
          const first = sample();
          const second = sample();
          resolve({ schema: 'nashura.backup-queue-readonly.v1', read_only: true,
            uid: uid, process_name: null, pid: Process.id, user_id: 0,
            service_class: className(service), user_class: className(user),
            target_uid: targetUid, first: first, second: second,
            samples_equal: JSON.stringify(first) === JSON.stringify(second),
            consistency: 'UNSYNCHRONIZED_TWO_SAMPLES',
            historical_branch: 'UNRESOLVED', catalog_ready_pass: false });
        } catch (error) {
          reject(new Error(String(error) + '\n' + error.stack));
        }
      });
    });
  }
};
