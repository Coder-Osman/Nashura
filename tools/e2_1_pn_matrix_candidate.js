'use strict';

let settings = null;
let api = null;
let libc = null;
const results = [];

function path(name) { return Memory.allocUtf8String(name); }

function seed(name, value) {
  const text = path(value);
  const length = unescape(encodeURIComponent(value)).length;
  const bytes = Memory.alloc(12 + length);
  bytes.writeByteArray([78, 83, 80, 78, 1, 0, 0, 0]);
  bytes.add(8).writeU32(length);
  bytes.add(12).writeByteArray(text.readByteArray(length));
  const descriptor = libc.open(path(name), 1 | 64 | 128 | 0x80000, 0o600);
  if (descriptor < 0) throw new Error('exclusive seed file required');
  try {
    if (libc.write(descriptor, bytes, 12 + length).toNumber() !== 12 + length || libc.fsync(descriptor) !== 0) {
      throw new Error('seed file write failed');
    }
  } finally { if (libc.close(descriptor) !== 0) throw new Error('seed close failed'); }
}

function disk(name) {
  const descriptor = libc.open(path(name), 0x80000, 0);
  if (descriptor < 0) throw new Error('candidate file missing');
  try {
    const bytes = Memory.alloc(4108);
    const amount = libc.read(descriptor, bytes, 4108).toNumber();
    if (amount < 12 || JSON.stringify(Array.from(new Uint8Array(bytes.readByteArray(8)))) !== '[78,83,80,78,1,0,0,0]') {
      throw new Error('candidate file format mismatch');
    }
    const length = bytes.add(8).readU32();
    if (length > 4095 || length + 12 !== amount) throw new Error('candidate file length mismatch');
    return length ? bytes.add(12).readUtf8String(length) : '';
  } finally { if (libc.close(descriptor) !== 0) throw new Error('disk read close failed'); }
}

function receipt(pointer) {
  return {changed: pointer.readU32(), committed: pointer.add(4).readU32(), failed: pointer.add(8).readU32(),
    renamed: pointer.add(12).readU32(), error_o: pointer.add(16).readS32(), error_p: pointer.add(20).readS32(),
    backup_requested: pointer.add(24).readU32()};
}

rpc.exports = {
  configure(config) {
    if (settings || Process.arch !== 'arm64' ||
        !/^\/data\/user\/0\/nullsroyale\.rel\.free\/e21f-[a-z0-9-]+$/.test(config.directory)) {
      throw new Error('exclusive matrix directory required');
    }
    settings = config;
    const bind = (name, result, types) => new NativeFunction(Module.getGlobalExportByName(name), result, types);
    libc = {mkdir: bind('mkdir', 'int', ['pointer', 'uint']), open: bind('open', 'int', ['pointer', 'int', 'uint']),
      write: bind('write', 'int64', ['int', 'pointer', 'ulong']), read: bind('read', 'int64', ['int', 'pointer', 'ulong']),
      close: bind('close', 'int', ['int']), fsync: bind('fsync', 'int', ['int'])};
    if (libc.mkdir(path(config.directory), 0o700) !== 0) throw new Error('new matrix directory required');
    const module = Module.load(config.library);
    const candidate = (name, types) => new NativeFunction(module.getExportByName('e21_pn_' + name), 'int', types);
    api = {open: candidate('open', ['pointer', 'pointer']), read: candidate('read', ['pointer', 'pointer', 'ulong', 'pointer']),
      write: candidate('write', ['pointer', 'pointer', 'pointer']), close: candidate('close', ['pointer']),
      free: new NativeFunction(module.getExportByName('e21_pn_free'), 'void', ['pointer'])};
    return true;
  },
  run(item) {
    if (!settings || results.length >= 16 || !/^[a-z0-9-]+$/.test(item.case) ||
        !['read', 'write'].includes(item.operation) || !Array.isArray(item.initial) || item.initial.length !== 2 ||
        [...item.initial, item.value || ''].some(value => typeof value !== 'string' || value.includes('\0') ||
          unescape(encodeURIComponent(value)).length > 4095)) throw new Error('bounded matrix case required');
    const directory = settings.directory + '/' + item.case;
    if (libc.mkdir(path(directory), 0o700) !== 0) throw new Error('new case directory required');
    ['o', 'p'].forEach((store, index) => seed(directory + '/pn-' + store + '.bin', item.initial[index]));
    const handle = Memory.alloc(Process.pointerSize);
    handle.writePointer(ptr(0));
    if (api.open(path(directory), handle) !== 0) throw new Error('candidate open failed');
    const context = handle.readPointer();
    const audit = Memory.alloc(28);
    const output = Memory.alloc(4096);
    const row = {case: item.case, initial: item.initial, operation: item.operation};
    results.push(row);
    try {
      row.status = item.operation === 'read' ? api.read(context, output, 4096, audit)
        : api.write(context, path(item.value), audit);
      row.receipt = receipt(audit);
      if (row.status !== 0 || row.receipt.failed) throw new Error('candidate operation failed');
      if (item.operation === 'read') row.value = output.readUtf8String();
    } finally {
      row.close_status = api.close(context);
      api.free(context);
    }
    if (row.close_status !== 0) throw new Error('candidate close failed');
    row.disk = ['o', 'p'].map(store => disk(directory + '/pn-' + store + '.bin'));
    return row;
  },
  snapshot() {
    return {schema: 'e2.1-pn-matrix-candidate.v1', results, original_storage_format_compatibility: false,
      seed_scope: 'test-only logical-state import into independent candidate files',
      native_bridge_replacement: false, no_vm_verified: false};
  }
};
