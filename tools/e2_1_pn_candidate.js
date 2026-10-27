'use strict';

let settings = null;
let api = null;
let context = ptr(0);
let closed = false;
const results = [];
const experiments = [];

function receipt(pointer) {
  return {changed: pointer.readU32(), committed: pointer.add(4).readU32(), failed: pointer.add(8).readU32(),
    renamed: pointer.add(12).readU32(), error_o: pointer.add(16).readS32(), error_p: pointer.add(20).readS32(),
    backup_requested: pointer.add(24).readU32()};
}

function open() {
  const output = Memory.alloc(Process.pointerSize);
  output.writePointer(ptr(0));
  const status = api.open(Memory.allocUtf8String(settings.directory), output);
  if (status !== 0 || output.readPointer().isNull()) throw new Error('candidate open failed: ' + status);
  context = output.readPointer();
  closed = false;
}

function write(value) {
  const output = Memory.alloc(28);
  const status = api.write(context, Memory.allocUtf8String(value), output);
  return {status, receipt: receipt(output)};
}

function read() {
  const output = Memory.alloc(4096);
  const audit = Memory.alloc(28);
  const status = api.read(context, output, 4096, audit);
  return {status, receipt: receipt(audit), value: status === 0 ? output.readUtf8String() : null};
}

function retire() {
  if (context.isNull()) return;
  const status = api.close(context);
  if (status !== 0) throw new Error('candidate close failed: ' + status);
  const after = read();
  if (after.status !== -2) throw new Error('closed candidate remained readable');
  api.free(context);
  context = ptr(0);
  closed = true;
  experiments.push({case: 'close-rejects-read', status: after.status});
}

function snapshot() {
  return {schema: 'e2.1-pn-candidate-replay.v1', results, experiments, closed,
    scope: 'isolated candidate after original full trajectory; original bridge remains installed',
    backup_delivery: 'unknown; receipt records intent only', concurrency_equivalence: 'unknown',
    original_storage_format_compatibility: false, replacement_verified: false, no_vm_verified: false};
}

rpc.exports = {
  configure(config) {
    if (settings || Process.arch !== 'arm64' || !config.directory.startsWith('/data/user/0/nullsroyale.rel.free/e21e-')) {
      throw new Error('exclusive candidate configuration required');
    }
    settings = config;
    const mkdir = new NativeFunction(Module.getGlobalExportByName('mkdir'), 'int', ['pointer', 'uint']);
    if (mkdir(Memory.allocUtf8String(config.directory), 0o700) !== 0) throw new Error('new candidate directory required');
    const module = Module.load(config.library);
    const bind = (name, types) => new NativeFunction(module.getExportByName('e21_pn_' + name), 'int', types);
    api = {open: bind('open', ['pointer', 'pointer']), read: bind('read', ['pointer', 'pointer', 'ulong', 'pointer']),
      write: bind('write', ['pointer', 'pointer', 'pointer']), close: bind('close', ['pointer']),
      free: new NativeFunction(module.getExportByName('e21_pn_free'), 'void', ['pointer'])};
    open();
    return true;
  },
  replay(originals) {
    if (!settings || results.length || originals.length < 4 || originals.length > 64) throw new Error('bounded single replay required');
    try {
      for (const original of originals) {
        if (original.case === 'saved' || original.case === 'restore-saved' || original.case === 'verify-restored') continue;
        if (original.key !== 'PN_POPUP' || !['read', 'write'].includes(original.operation)) throw new Error('unexpected oracle row');
        const observed = original.operation === 'write' ? write(original.input) : read();
        const row = {case: original.case, operation: original.operation, candidate: observed,
          original_exception_state: original.exception_state};
        results.push(row);
        if (observed.status !== 0 || observed.receipt.failed || original.exception_state.before !== false
            || original.exception_state.after !== false) throw new Error('candidate or oracle failure');
        if (original.operation === 'read') {
          row.original_value = original.result.value;
          row.equal = observed.value === original.result.value;
          if (!row.equal) throw new Error('candidate differs from original: ' + original.case);
        }
      }
      const durable = read();
      retire();
      open();
      const reopened = read();
      experiments.push({case: 'reopen', before: durable, after: reopened,
        equal: reopened.status === 0 && reopened.value === durable.value});
      if (!experiments[experiments.length - 1].equal) throw new Error('candidate reopen differs');
      const setup = write('candidate-durable');
      experiments.push({case: 'durable-setup', result: setup, observed: read()});
      if (setup.receipt.committed !== 3) throw new Error('durable setup failed');
      const chmod = new NativeFunction(Module.getGlobalExportByName('chmod'), 'int', ['pointer', 'uint']);
      const directory = Memory.allocUtf8String(settings.directory);
      if (chmod(directory, 0o500) !== 0) throw new Error('fault permission setup failed');
      try {
        const failed = write('candidate-volatile');
        const visible = read();
        const same = write('candidate-volatile');
        const row = {case: 'real-permission-failure', failed, visible, same};
        experiments.push(row);
        if (failed.status !== 0 || failed.receipt.failed !== 3 || failed.receipt.committed !== 0
            || !failed.receipt.error_o || !failed.receipt.error_p || visible.value !== 'candidate-volatile'
            || same.receipt.changed !== 0) throw new Error('candidate failure visibility differs');
      } finally {
        if (chmod(directory, 0o700) !== 0) throw new Error('candidate permission restoration failed');
      }
      retire();
      open();
      const afterFailure = read();
      experiments.push({case: 'reopen-after-failure', observed: afterFailure,
        equal: afterFailure.value === 'candidate-durable'});
      if (afterFailure.value !== 'candidate-durable') throw new Error('failed write unexpectedly persisted');
      retire();
      return snapshot();
    } finally {
      if (!context.isNull()) retire();
    }
  },
  snapshot
};
