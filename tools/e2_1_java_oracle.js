'use strict';

let settings = null;
let active = false;
const events = [];

function debugCall(name, signature, argumentsBuilder) {
  const module = Process.getModuleByName(settings.engine_module);
  const attach = new NativeFunction(module.base.add(settings.jni_attach), 'pointer', []);
  const environment = attach();
  if (environment.isNull()) throw new Error('JNI environment unavailable');
  const table = environment.readPointer();
  const jni = (slot, result, argumentsTypes) => new NativeFunction(
    table.add(slot * Process.pointerSize).readPointer(), result, ['pointer', ...argumentsTypes]);
  const pending = jni(228, 'uchar', []);
  if (pending(environment)) throw new Error('preexisting pending exception; left unchanged');
  const locals = [];
  const checked = result => {
    if (pending(environment)) throw new Error('oracle JNI exception; left pending');
    return result;
  };
  try {
    const type = checked(jni(6, 'pointer', ['pointer'])(environment, Memory.allocUtf8String('android/os/Debug')));
    if (type.isNull()) throw new Error('Debug class unavailable');
    locals.push(type);
    const method = checked(jni(113, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, type,
      Memory.allocUtf8String(name), Memory.allocUtf8String(signature)));
    if (method.isNull()) throw new Error('Debug method unavailable');
    const args = argumentsBuilder ? argumentsBuilder(environment, jni, checked, locals) : ptr(0);
    checked(jni(143, 'void', ['pointer', 'pointer', 'pointer'])(environment, type, method, args));
    events.push({operation: name, thread: Process.getCurrentThreadId(), status: 'returned_without_pending_exception'});
  } finally {
    for (const reference of locals.reverse()) jni(23, 'void', ['pointer'])(environment, reference);
  }
}

rpc.exports = {
  configure(config) {
    if (settings || config.engine_module !== 'libg.so' || !Number.isInteger(config.jni_attach) ||
        !config.trace_path.startsWith('/data/user/0/') || !config.trace_path.endsWith('.trace')) {
      throw new Error('invalid immutable Java oracle contract');
    }
    settings = config;
    return true;
  },
  start() {
    if (!settings || active) throw new Error('trace already active or unconfigured');
    debugCall('startMethodTracing', '(Ljava/lang/String;II)V', (environment, jni, checked, locals) => {
      const path = checked(jni(167, 'pointer', ['pointer'])(environment, Memory.allocUtf8String(settings.trace_path)));
      if (path.isNull()) throw new Error('trace path allocation failed');
      locals.push(path);
      const args = Memory.alloc(24);
      args.writePointer(path);
      args.add(8).writeS32(16 * 1024 * 1024);
      args.add(16).writeS32(0);
      return args;
    });
    active = true;
    return true;
  },
  stop() {
    if (!active) throw new Error('trace not active');
    debugCall('stopMethodTracing', '()V');
    active = false;
    return true;
  },
  snapshot() {
    return {schema: 'e2.1-java-oracle.v1', active, events,
      scope: 'explicit bounded post-cold-ready probes only; no cold-start or exhaustive method coverage inferred',
      no_vm_verified: false, training_admissible: false};
  }
};
