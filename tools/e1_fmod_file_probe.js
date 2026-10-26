'use strict';

let settings = null;
let api = null;
const events = [];
const listeners = [];

function location(address) {
  const module = Process.findModuleByAddress(address);
  return module ? module.name + '+' + address.sub(module.base) : address.toString();
}

rpc.exports = {
  configure(config) {
    if (settings) throw new Error('configuration is immutable');
    const module = Process.getModuleByName(config.module);
    api = {
      open: new NativeFunction(module.getExportByName('FMOD_OS_File_Open'),
        'int', ['pointer', 'int', 'pointer', 'pointer']),
      read: new NativeFunction(module.getExportByName('FMOD_OS_File_Read'),
        'int', ['pointer', 'pointer', 'uint', 'pointer']),
      seek: new NativeFunction(module.getExportByName('FMOD_OS_File_Seek'),
        'int', ['pointer', 'uint']),
      close: new NativeFunction(module.getExportByName('FMOD_OS_File_Close'),
        'int', ['pointer'])
    };
    for (const entry of config.targets) {
      const target = Process.getModuleByName(entry.module).getExportByName(entry.symbol);
      listeners.push(Interceptor.attach(target, {
        onEnter(args) {
          this.row = {name: entry.name, target: location(target),
            caller: location(this.returnAddress), thread: Process.getCurrentThreadId()};
          if (entry.name === 'AAssetManager_open') this.row.path = args[1].readUtf8String();
          events.push(this.row);
        },
        onLeave(result) { this.row.result = result.toString(); }
      }));
    }
    Interceptor.flush();
    settings = config;
  },
  query(path) {
    if (!settings) throw new Error('not configured');
    if (typeof path !== 'string' || path.includes('\0') || path.length > 1024)
      throw new Error('invalid diagnostic path');
    const size = Memory.alloc(4);
    const handle = Memory.alloc(Process.pointerSize);
    const output = Memory.alloc(65536);
    const amount = Memory.alloc(4);
    size.writeU32(0);
    handle.writePointer(ptr(0));
    const begin = events.length;
    const opened = api.open(Memory.allocUtf8String(path), 0, size, handle);
    const result = {path, open_result: opened, length: size.readU32(), operations: []};
    const file = handle.readPointer();
    if (opened === 0 && file.isNull()) throw new Error('successful open without file');
    if (opened !== 0 && !file.isNull()) throw new Error('failed open retained file');
    if (!file.isNull()) {
      try {
        if (result.length > 65535) throw new Error('diagnostic capacity exceeded');
        amount.writeU32(0);
        result.read_result = api.read(file, output, result.length, amount);
        result.read_bytes = amount.readU32();
        if (result.read_bytes > result.length) throw new Error('read exceeded capacity');
        result.bytes = Array.from(new Uint8Array(output.readByteArray(result.read_bytes)));
        for (const offset of [0, result.length, result.length + 1]) {
          const seek = api.seek(file, offset);
          amount.writeU32(0);
          const read = api.read(file, output, 1, amount);
          result.operations.push({offset, seek, read, amount: amount.readU32()});
        }
      } finally { result.close_result = api.close(file); }
    }
    result.events = events.slice(begin);
    return result;
  },
  snapshot() { return {schema: 'e1-fmod-file-diagnostic.v1', events,
    scope: 'explicit FMOD OS file entry diagnostic; not natural battle audio coverage'}; }
};
