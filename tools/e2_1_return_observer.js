'use strict';

let settings = null;
let helper = null;
let commandMethod = null;
const events = [];

function invoke(operation) {
  const engine = Process.getModuleByName('libg.so');
  const environment = new NativeFunction(engine.base.add(settings.jni_attach), 'pointer', [])();
  if (environment.isNull()) throw new Error('original JNI attach returned null');
  const table = environment.readPointer();
  const jni = (slot, result, types) => new NativeFunction(table.add(slot * Process.pointerSize).readPointer(),
    result, ['pointer', ...types]);
  const pending = jni(228, 'uchar', []);
  const locals = [];
  const checked = result => {
    if (pending(environment)) throw new Error('JNI exception left pending; stop owned attempt');
    return result;
  };
  if (pending(environment)) throw new Error('preexisting JNI exception left pending');
  const local = result => { checked(result); if (result.isNull()) throw new Error('missing JNI object'); locals.push(result); return result; };
  const text = value => local(jni(167, 'pointer', ['pointer'])(environment, Memory.allocUtf8String(value)));
  const find = name => local(jni(6, 'pointer', ['pointer'])(environment, Memory.allocUtf8String(name)));
  const method = (type, name, signature, slot) => {
    const address = checked(jni(slot, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, type,
      Memory.allocUtf8String(name), Memory.allocUtf8String(signature)));
    if (address.isNull()) throw new Error('missing JNI method');
    return address;
  };
  const args = values => {
    const buffer = Memory.alloc(Math.max(8, values.length * 8));
    values.forEach((value, index) => buffer.add(index * 8).writePointer(value));
    return buffer;
  };
  try {
    if (!helper) {
      const threadType = find('android/app/ActivityThread');
      const current = method(threadType, 'currentApplication', '()Landroid/app/Application;', 113);
      const application = local(jni(116, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, threadType, current, ptr(0)));
      const contextType = find('android/content/Context');
      const loaderMethod = method(contextType, 'getClassLoader', '()Ljava/lang/ClassLoader;', 33);
      const parent = local(jni(36, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, application, loaderMethod, ptr(0)));
      const dexType = find('dalvik/system/DexClassLoader');
      const constructor = method(dexType, '<init>', '(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/ClassLoader;)V', 33);
      const loader = local(jni(30, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, dexType, constructor,
        args([text(settings.dex_path), text(settings.optimized_directory), ptr(0), parent])));
      const loaderType = find('java/lang/ClassLoader');
      const loadMethod = method(loaderType, 'loadClass', '(Ljava/lang/String;)Ljava/lang/Class;', 33);
      const type = local(jni(36, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, loader, loadMethod,
        args([text('org.nashura.evidence.E21PreferenceObserver')])));
      helper = checked(jni(21, 'pointer', ['pointer'])(environment, type));
      if (helper.isNull()) throw new Error('global helper class allocation failed');
      commandMethod = method(type, 'command', '(Ljava/lang/String;)Ljava/lang/String;', 113);
    }
    const output = local(jni(116, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, helper, commandMethod,
      args([text(operation)])));
    const characters = checked(jni(169, 'pointer', ['pointer', 'pointer'])(environment, output, ptr(0)));
    if (characters.isNull()) throw new Error('output string unavailable');
    let result;
    try { result = characters.readUtf8String(); }
    finally { jni(170, 'void', ['pointer', 'pointer'])(environment, output, characters); }
    events.push({operation, status: 'returned', thread: Process.getCurrentThreadId()});
    return result;
  } finally {
    for (const reference of locals.reverse()) jni(23, 'void', ['pointer'])(environment, reference);
  }
}

rpc.exports = {
  configure(config) {
    if (settings || config.jni_attach !== 0x17d8ae8 ||
        !config.dex_path.startsWith('/data/user/0/nullsroyale.rel.free/') || !config.dex_path.endsWith('.dex') ||
        !config.optimized_directory.startsWith('/data/user/0/nullsroyale.rel.free/')) {
      throw new Error('invalid immutable return observer configuration');
    }
    settings = config;
    return true;
  },
  command(operation) {
    if (!settings || !['install', 'snapshot', 'uninstall', 'files', 'read-stores'].includes(operation)) throw new Error('invalid command');
    return invoke(operation);
  },
  snapshot() {
    return {events, scope: 'two temporary forwarding SharedPreferences wrappers; explicit post-cold-ready windows only',
      coverage: 'actual delegated return values only; no JVMTI or exhaustive Java coverage', no_vm_verified: false};
  }
};
