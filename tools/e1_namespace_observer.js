'use strict';

let settings = null;
const events = [];
const errors = [];
const listeners = [];

function textAt(address) {
  return address.readUtf8String();
}

function engineString(address) {
  return (address.readU8() & 1) === 0
    ? textAt(address.add(1)) : textAt(address.add(16).readPointer());
}

rpc.exports = {
  configure(config) {
    if (settings) throw new Error('configuration is immutable');
    settings = config;
    const module = Process.getModuleByName(config.module);
    for (const entry of config.entries) {
      try {
        listeners.push(Interceptor.attach(module.base.add(entry.offset), {
          onEnter(args) {
            this.row = {name: entry.name, thread: Process.getCurrentThreadId(),
              caller: this.returnAddress.sub(module.base).toString(),
              receiver: args[0].toString()};
            try {
              this.row.path = entry.string_kind === 'engine'
                ? engineString(args[1]) : textAt(args[1]);
            } catch (error) { this.row.path_error = String(error); }
            events.push(this.row);
          },
          onLeave(result) { this.row.result = result.toString(); }
        }));
      } catch (error) { errors.push({name: entry.name, error: String(error)}); }
    }
    Interceptor.flush();
    return true;
  },
  inspect() {
    const module = Process.getModuleByName(settings.module);
    const filesystem = module.base.add(settings.filesystem_global).readPointer();
    function describe(object) {
      return {object: object.toString(), vtable: object.readPointer().sub(module.base).toString(),
        root: engineString(object.add(8)), namespace: engineString(object.add(32))};
    }
    const providers = [];
    const start = filesystem.add(0x118).readPointer();
    const end = filesystem.add(0x120).readPointer();
    if (end.sub(start).toUInt32() > 4096) throw new Error('unexpected provider vector');
    for (let cursor = start; cursor.compare(end) < 0; cursor = cursor.add(16)) {
      const object = cursor.readPointer();
      providers.push(describe(object));
    }
    return {filesystem: filesystem.toString(), flag: filesystem.add(0x180).readU8(), providers,
      apk: describe(filesystem.add(0x160).readPointer()),
      disk: describe(filesystem.add(0x170).readPointer())};
  },
  query(path) {
    if (!settings.probe_path) throw new Error('native diagnostic helper not configured');
    const module = Process.getModuleByName(settings.module);
    const filesystem = module.base.add(settings.filesystem_global).readPointer();
    const helper = Module.load(settings.probe_path);
    helper.ensureInitialized();
    const resolve = new NativeFunction(helper.getExportByName('e1_resolve_path'),
      'int', ['pointer', 'pointer', 'pointer', 'pointer', 'pointer', 'ulong']);
    const read = new NativeFunction(helper.getExportByName('e1_read_resource'),
      'int64', ['pointer', 'pointer', 'pointer', 'pointer', 'int', 'pointer']);
    const exercise = new NativeFunction(helper.getExportByName('e1_exercise_resource'),
      'int', ['pointer', 'pointer', 'pointer', 'pointer']);
    const name = Memory.allocUtf8String(path);
    if ([...path].some(character => character.charCodeAt(0) > 127) || path.includes('\0'))
      throw new Error('diagnostic path must be NUL-free ASCII');
    const size = path.length;
    const encoded = Memory.alloc(24);
    if (size < 23) {
      encoded.writeU8(size * 2);
      encoded.add(1).writeUtf8String(path);
    } else {
      encoded.writeU64((size + 16) | 1);
      encoded.add(8).writeU64(size);
      encoded.add(16).writePointer(name);
    }
    const output = Memory.alloc(65536);
    const details = Memory.alloc(32);
    const begin = events.length;
    let resolvedPath = null;
    if (settings.resolve_offset !== undefined) {
      let resolved;
      try {
        resolved = resolve(module.base.add(settings.resolve_offset),
          module.base.add(settings.delete_offset), filesystem, name, output, 65536);
      } catch (error) {
        return {path, resolve_error: String(error), context: error.context,
          memory: error.memory, events: events.slice(begin)};
      }
      if (resolved < 0) throw new Error('resolved path exceeds diagnostic capacity');
      resolvedPath = output.readUtf8String();
    }
    const amount = read(module.base.add(settings.open_offset), filesystem, encoded, output, 65536, details);
    const length = Number(amount.toString());
    const operations = Memory.alloc(24 * 8);
    const operationStatus = exercise(module.base.add(settings.open_offset), filesystem, encoded, operations);
    return {path, resolved_path: resolvedPath, read_result: length,
      operation_status: operationStatus,
      operations: Array.from({length: 24}, (_, index) => operations.add(index * 8).readS64().toString()),
      details: [0, 8, 16, 24].map(offset => details.add(offset).readS64().toString()),
      bytes: length >= 0 ? Array.from(new Uint8Array(output.readByteArray(length))) : null,
      events: events.slice(begin)};
  },
  snapshot() { return {schema: 'e1-engine-namespace-observation.v1', events, errors}; }
};
