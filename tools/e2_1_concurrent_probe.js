'use strict';

let settings = null;
let attempted = false;
let outcome = null;

rpc.exports = {
  configure(config) {
    if (settings || Process.arch !== 'arm64' || config.engine_module !== 'libg.so' ||
        typeof config.helper !== 'string' || !config.helper.startsWith('/data/app/') ||
        !config.helper.endsWith('/lib/arm64/libe21concurrent.so')) {
      throw new Error('invalid immutable concurrent probe contract');
    }
    const engine = Process.getModuleByName(config.engine_module);
    for (const name of ['string_constructor', 'string_destructor', 'read_bridge', 'write_bridge', 'jni_attach']) {
      if (!Number.isInteger(config[name])) throw new Error('missing verified entry');
      const range = Process.findRangeByAddress(engine.base.add(config[name]));
      if (!range || !range.protection.includes('x')) throw new Error('entry not executable');
    }
    settings = config;
    return true;
  },
  run(value) {
    if (!settings || attempted || typeof value !== 'string' || !/^[0-9]{1,8}$/.test(value)) {
      throw new Error('single bounded PN_POPUP probe requires saved decimal value');
    }
    attempted = true;
    const engine = Process.getModuleByName(settings.engine_module);
    const helper = Module.load(settings.helper);
    const call = new NativeFunction(helper.getExportByName('e21_concurrent'), 'int', Array(7).fill('pointer'));
    const result = new NativeFunction(helper.getExportByName('e21_worker_result'), 'pointer', ['int']);
    const addresses = ['string_constructor', 'string_destructor', 'read_bridge', 'write_bridge', 'jni_attach']
      .map(name => engine.base.add(settings[name]));
    const status = call(...addresses, Memory.allocUtf8String('PN_POPUP'), Memory.allocUtf8String(value));
    const workers = [0, 1].map(index => {
      const row = result(index);
      if (row.isNull()) throw new Error('worker evidence missing');
      return {thread: row.readS32(), completed: row.add(4).readS32(),
        error: row.add(8).readS32(), detached: row.add(12).readS32() === 1};
    });
    outcome = {status, workers, writes: 'same saved PN value only',
      scope: 'two newly owned pthreads, at most four write/read pairs each; no general thread-safety claim',
      exception_handling: 'pending exceptions detected, never cleared; helper-owned threads detached',
      no_vm_verified: false, training_admissible: false};
    return outcome;
  },
  snapshot() {
    return {attempted, outcome};
  }
};
