'use strict';

let settings = null;
let attempted = false;
let outcome = null;

rpc.exports = {
  configure(config) {
    if (settings || Process.arch !== 'arm64' || config.engine_module !== 'libg.so' ||
        !config.helper.startsWith('/data/app/') || !config.helper.endsWith('/lib/arm64/libe21race.so')) {
      throw new Error('invalid immutable race probe contract');
    }
    const engine = Process.getModuleByName(config.engine_module);
    for (const name of ['string_constructor', 'string_destructor', 'read_bridge', 'write_bridge', 'jni_attach']) {
      if (!Number.isInteger(config[name])) throw new Error('missing verified native entry');
      const range = Process.findRangeByAddress(engine.base.add(config[name]));
      if (!range || !range.protection.includes('x')) throw new Error('entry not executable');
    }
    settings = config;
    return true;
  },
  run() {
    if (!settings || attempted) throw new Error('single bounded race probe only');
    attempted = true;
    const engine = Process.getModuleByName(settings.engine_module);
    const helper = Module.load(settings.helper);
    const call = new NativeFunction(helper.getExportByName('e21_race'), 'int', Array(5).fill('pointer'));
    const result = new NativeFunction(helper.getExportByName('e21_race_result'), 'pointer', ['int']);
    const status = call(...['string_constructor', 'string_destructor', 'read_bridge', 'write_bridge', 'jni_attach']
      .map(name => engine.base.add(settings[name])));
    const workers = [0, 1].map(index => {
      const row = result(index);
      if (row.isNull()) throw new Error('worker result missing');
      return {thread: row.readS32(), completed: row.add(4).readS32(), error: row.add(8).readS32(),
        detached: row.add(12).readS32() === 1, own_reads: row.add(16).readS32(),
        other_reads: row.add(20).readS32(), unknown_reads: row.add(24).readS32(), index: row.add(28).readS32()};
    });
    outcome = {status, workers, scope: 'two owned pthreads; four barrier-aligned write/read rounds; distinct controlled values',
      exception_handling: 'pending exceptions detected, never cleared; helper threads detached',
      general_thread_safety: 'unknown', no_vm_verified: false};
    return outcome;
  },
  snapshot() { return {attempted, outcome}; }
};
