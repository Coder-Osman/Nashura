'use strict';

const events = [], coverage = [], errors = [];
let lost = 0;
function record(event) {
  if (events.length >= 30000) { lost++; return; }
  event.sequence = events.length;
  event.thread = Process.getCurrentThreadId();
  events.push(event);
}
function boundedString(p) {
  if (p.isNull()) return null;
  try {
    let value = '';
    for (let index = 0; index < 1024; index++) {
      const byte = p.add(index).readU8();
      if (!byte) return value;
      if (byte > 127) return null;
      value += String.fromCharCode(byte);
    }
  } catch (_) { return null; }
  return null;
}
function engineString(p) {
  try {
    const object = p.and(ptr('0x00ffffffffffffff'));
    const tag = object.readU8();
    const length = tag & 1 ? object.add(8).readU64().toNumber() : tag >> 1;
    if (length < 0 || length > 1023) return null;
    const data = tag & 1 ? object.add(16).readPointer().and(ptr('0x00ffffffffffffff')) : object.add(1);
    const value = boundedString(data);
    return value !== null && value.length === length ? value : null;
  } catch (_) { return null; }
}
function attach(module, offset, label, callbacks) {
  try {
    const address = module.base.add(offset);
    if (!Process.findRangeByAddress(address).protection.includes('x')) throw new Error('not executable');
    Interceptor.attach(address, callbacks);
    coverage.push({label: label, installed: true});
  } catch (error) { coverage.push({label: label, installed: false}); errors.push(label + ': ' + String(error)); }
}
function observe(module) {
  if (module.name !== 'libg.so') return;
  attach(module, 0xe1bdcc, 'table_batch', {
    onEnter: function (args) {
      let listCount = null;
      try { listCount = args[1].and(ptr('0x00ffffffffffffff')).add(12).readS32(); } catch (_) {}
      record({kind: 'table_batch', args: [2, 3, 4, 5, 6].map(i => args[i].toUInt32()), listCount: listCount,
        caller: this.returnAddress.sub(module.base).toString()});
    }
  });
  attach(module, 0xdcd364, 'postprocess', {
    onEnter: function () { record({kind: 'postprocess', caller: this.returnAddress.sub(module.base).toString()}); }
  });
  attach(module, 0x12e03e4, 'filesystem_open', {
    onEnter: function (args) {
      this.path = engineString(args[1]);
    },
    onLeave: function (value) {
      record({kind: 'filesystem_open', path: this.path, nonnull: !value.isNull()});
    }
  });
  record({kind: 'engine_hooks_ready'});
}
rpc.exports = {
  install: function () {
    Process.attachModuleObserver({onAdded: observe});
    const android = Process.findModuleByName('libandroid.so');
    if (android) {
      const open = android.findExportByName('AAssetManager_open');
      if (open) {
        Interceptor.attach(open, {onEnter: function (args) { record({kind: 'asset_open', path: boundedString(args[1])}); }});
        coverage.push({label: 'asset_open', installed: true});
      }
    }
    return {coverage: coverage, errors: errors};
  },
  snapshot: function () { return {events: events, coverage: coverage, errors: errors, lost: lost}; }
};
