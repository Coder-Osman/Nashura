'use strict';

function javaFilePath(value) {
  if (value === null) return null;
  const File = Java.use('java.io.File');
  const file = Java.cast(value, File);
  return String(file.getAbsolutePath());
}

function getField(object, name) {
  let clazz = object.getClass();
  while (clazz !== null) {
    try {
      const field = clazz.getDeclaredField(name);
      field.setAccessible(true);
      return field.get.overload('java.lang.Object').call(field, object);
    } catch (error) {
      if (String(error).indexOf('NoSuchFieldException') === -1) throw error;
      clazz = clazz.getSuperclass();
    }
  }
  throw new Error('field not found in class hierarchy: ' + name);
}

function preferenceFiles(preferences) {
  const output = { className: String(preferences.getClass().getName()) };
  ['mFile', 'mBackupFile'].forEach(function (name) {
    try {
      output[name] = javaFilePath(getField(preferences, name));
    } catch (error) {
      output[name] = { unavailable: String(error) };
    }
  });
  return output;
}

rpc.exports = {
  inspect: function () {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          const app = Java.use('com.supercell.titan.GameApp').getInstance();
          const result = {};
          ['c', 'o', 'p'].forEach(function (slot) {
            const wrapper = getField(app, slot);
            const preferences = getField(wrapper, 'd');
            result[slot] = preferenceFiles(preferences);
            result[slot].loading_key_present = Java.cast(
              preferences, Java.use('android.content.SharedPreferences')
            ).contains('LOADING_SCREEN_STATE_LABEL');
          });
          resolve(result);
        } catch (error) {
          reject(new Error('preference inspection failed: ' + String(error)));
        }
      });
    });
  }
};
