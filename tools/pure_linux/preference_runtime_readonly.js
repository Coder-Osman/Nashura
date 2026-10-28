'use strict';

function reflectedField(object, name) {
  let clazz = object.getClass();
  while (clazz !== null) {
    try {
      const member = clazz.getDeclaredField(name);
      member.setAccessible(true);
      return member.get.overload('java.lang.Object').call(member, object);
    } catch (error) {
      if (String(error).indexOf('NoSuchFieldException') === -1) throw error;
      clazz = clazz.getSuperclass();
    }
  }
  throw new Error('missing field: ' + name);
}

function byteValues(value) {
  const ArrayClass = Java.use('java.lang.reflect.Array');
  const values = [];
  for (let index = 0; index < ArrayClass.getLength(value); index++) {
    values.push(ArrayClass.getByte(value, index));
  }
  return values;
}

function digestBytes(value) {
  const bytes = Java.array('byte', byteValues(value));
  const digest = Java.use('java.security.MessageDigest').getInstance('SHA-256').digest(bytes);
  let hex = '';
  for (let index = 0; index < digest.length; index++) {
    hex += ('0' + (digest[index] & 255).toString(16)).slice(-2);
  }
  return hex;
}

function cipherSummary(value) {
  const cipher = Java.cast(value, Java.use('javax.crypto.Cipher'));
  const spi = reflectedField(value, 'spi');
  const key = reflectedField(spi, 'encodedKey');
  const iv = cipher.getIV();
  return {
    algorithm: String(cipher.getAlgorithm()),
    provider: String(cipher.getProvider().getName()),
    implementation: String(spi.getClass().getName()),
    key_bytes: Java.use('java.lang.reflect.Array').getLength(key),
    key_sha256: digestBytes(key),
    iv_bytes: iv === null ? 0 : iv.length,
    iv_sha256: iv === null ? null : digestBytes(iv),
    encrypting: String(reflectedField(spi, 'encrypting')) === 'true',
    called_update: String(reflectedField(spi, 'calledUpdate')) === 'true'
  };
}

rpc.exports = {
  inspect: function () {
    return new Promise(function (resolve, reject) {
      Java.perform(function () {
        try {
          const app = Java.use('com.supercell.titan.GameApp').getInstance();
          const StringClass = Java.use('java.lang.String');
          const utf8 = StringClass.$new('UTF-8');
          const androidId = Java.use('android.provider.Settings$Secure').getString(
            app.getContentResolver(), 'android_id');
          if (androidId === null || !/^[0-9a-f]{16}$/.test(String(androidId))) {
            throw new Error('identity outside reviewed device domain');
          }
          const idBytes = StringClass.$new(String(androidId)).getBytes(utf8);
          const derived = Java.use('java.security.MessageDigest').getInstance('SHA-256').digest(idBytes);
          const info = app.getApplicationInfo();
          const result = {
            schema: 'nashura.preference-runtime-readonly.v1',
            pid: Process.id,
            package: String(app.getPackageName()),
            charset: String(Java.use('java.nio.charset.Charset').defaultCharset().name()),
            locale: String(Java.use('java.util.Locale').getDefault().toLanguageTag()),
            source_apk: String(info.sourceDir.value),
            native_libg: String(info.nativeLibraryDir.value) + '/libg.so',
            device_derivation: 'SHA256(Settings.Secure android_id UTF-8)',
            device_key_sha256: digestBytes(derived),
            stores: [],
            stores_issued: 0,
            backup_requests_issued: 0,
            original_cipher_calls_issued: 0
          };
          ['o', 'p'].forEach(function (slot) {
            const wrapper = reflectedField(app, slot);
            const preferences = reflectedField(wrapper, 'd');
            const file = Java.cast(reflectedField(preferences, 'mFile'), Java.use('java.io.File'));
            const entry = { slot: slot, path: String(file.getAbsolutePath()), ciphers: {} };
            ['a', 'b', 'c'].forEach(function (name) {
              entry.ciphers[name] = cipherSummary(reflectedField(wrapper, name));
            });
            result.stores.push(entry);
          });
          const material = [];
          ['o', 'p'].forEach(function (slot) {
            const cipher = reflectedField(reflectedField(app, slot), 'a');
            const spi = reflectedField(cipher, 'spi');
            Array.prototype.push.apply(material, byteValues(reflectedField(spi, 'encodedKey')));
          });
          const first = Java.cast(reflectedField(reflectedField(app, 'o'), 'a'),
            Java.use('javax.crypto.Cipher'));
          Array.prototype.push.apply(material, byteValues(first.getIV()));
          result.private_material_hex = material.map(function (value) {
            return ('0' + (value & 255).toString(16)).slice(-2);
          }).join('');
          resolve(result);
        } catch (error) {
          reject(new Error('read-only identity observation failed: ' + String(error)));
        }
      });
    });
  }
};
