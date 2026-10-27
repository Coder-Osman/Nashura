'use strict';

let settings = null;
const watched = new Map();
const retired = new Map();
const retained = [];
const events = [];

rpc.exports = {
  configure(config) {
    if (settings || Process.arch !== 'arm64') throw new Error('immutable AArch64 probe');
    settings = config;
    retained.push(Interceptor.attach(Process.getModuleByName('libc.so').getExportByName('free'), {
      onEnter(args) {
        const key = args[0].and('0x00ffffffffffffff').toString();
        if (watched.has(key)) {
          const name = watched.get(key);
          retired.set(name, (retired.get(name) || 0) + 1);
          events.push({case: name, event: 'libc.free', thread: Process.getCurrentThreadId()});
        }
      }
    }));
    return true;
  },
  run() {
    if (events.length) throw new Error('single-use lifecycle experiment');
    const engine = Process.getModuleByName(settings.engine_module);
    const construct = new NativeFunction(engine.base.add(settings.string_constructor), 'void', ['pointer', 'pointer']);
    const destroy = new NativeFunction(engine.base.add(settings.string_destructor), 'void', ['pointer']);
    const rows = [];
    for (const [name, value] of [['empty', ''], ['inline_one', 'a'], ['inline_seven', '1234567'],
      ['heap_eight', '12345678'], ['utf8_inline', '中𐐀'], ['utf8_heap', '中文𐐀'], ['heap_4095', 'x'.repeat(4095)]]) {
      const object = Memory.alloc(16);
      const source = Memory.allocUtf8String(value);
      retained.push(object, source);
      construct(object, source);
      const length = object.add(4).readU32();
      const heap = length >= 8;
      const data = heap ? object.add(8).readPointer().and('0x00ffffffffffffff') : object.add(8);
      if (data.readUtf8String(length) !== value || data.add(length).readU8() !== 0) throw new Error('constructor mismatch');
      if (heap) watched.set(data.toString(), name);
      destroy(object);
      const count = retired.get(name) || 0;
      rows.push({case: name, length, heap, frees_observed: count,
        expected_frees: heap ? 1 : 0, status: count === (heap ? 1 : 0) ? 'verified' : 'unknown'});
      if (heap) watched.delete(data.toString());
    }
    return {schema: 'e2.1-string-lifecycle.v1', rows, events,
      scope: 'own EngineString16 objects only; no Java reference retirement claim',
      use_after_free: 'not invoked; retired objects are invalid', java_reference_lifecycle: 'unknown'};
  },
  snapshot() { return {events, java_reference_lifecycle: 'unknown'}; }
};
