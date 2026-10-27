'use strict';

let settings = null;
let phase = 'cold-start';
let armed = false;
let sequence = 0;
let calls = 0;
let dropped = 0;
let moduleObserver = null;
const listeners = [];
const events = [];
const errors = [];
const active = new Map();
const pending = new Map();
const returnSites = new Map();

function record(event) {
  if (events.length >= settings.max_events) { dropped++; return; }
  const row = Object.assign({sequence: ++sequence, wall_milliseconds: Date.now(), phase}, event);
  events.push(row);
  send({event: 'pn_timeline', detail: row});
}

function watchReturn(address) {
  const site = address.toString();
  if (returnSites.has(site)) return;
  const range = Process.findRangeByAddress(address);
  if (!range || !range.protection.includes('x') || returnSites.size >= 64) {
    throw new Error('PN caller return instruction outside bounded executable coverage');
  }
  returnSites.set(site, Interceptor.attach(address, {
    onEnter() {
      const thread = Process.getCurrentThreadId();
      const stack = pending.get(thread) || [];
      const row = stack[stack.length - 1];
      if (!row || row.site !== site) return;
      stack.pop();
      record({edge: 'leave', call: row.call, operation: row.operation, thread,
        completion: 'observed caller return instruction'});
      active.delete(row.call);
      if (!stack.length) pending.delete(thread);
    }
  }));
  Interceptor.flush();
}

function arm(module) {
  if (module.name !== 'libg.so' || armed) return;
  for (const [operation, offset] of [['read', settings.read_bridge], ['write', settings.write_bridge]]) {
    const target = module.base.add(offset);
    const range = Process.findRangeByAddress(target);
    if (!range || !range.protection.includes('x')) throw new Error('PN timeline target is not executable');
    listeners.push(Interceptor.attach(target, {
      onEnter(args) {
        try {
          if (args[0].add(4).readU32() !== 8) return;
          const data = args[0].add(8).readPointer().and('0x00ffffffffffffff');
          if (data.isNull() || data.readUtf8String(8) !== 'PN_POPUP' || data.add(8).readU8() !== 0) return;
          watchReturn(this.returnAddress);
          const thread = Process.getCurrentThreadId();
          const row = {call: ++calls, operation, thread,
            caller: this.returnAddress.compare(module.base) >= 0 && this.returnAddress.compare(module.base.add(module.size)) < 0
              ? 'libg.so+' + this.returnAddress.sub(module.base) : 'external-probe',
            overlaps: Array.from(active.keys())};
          active.set(row.call, row);
          const stack = pending.get(thread) || [];
          stack.push({call: row.call, operation, site: this.returnAddress.toString()});
          pending.set(thread, stack);
          record(Object.assign({edge: 'enter'}, row));
        } catch (failure) {
          const range = Process.findRangeByAddress(this.returnAddress);
          const error = {error: String(failure), operation, thread: Process.getCurrentThreadId(), phase,
            return_address: this.returnAddress.toString(),
            caller: this.returnAddress.compare(module.base) >= 0 && this.returnAddress.compare(module.base.add(module.size)) < 0
              ? 'libg.so+' + this.returnAddress.sub(module.base) : 'external-probe',
            executable_range: range ? range.protection : null, completion: 'unknown'};
          errors.push(error);
          send({event: 'pn_timeline_error', detail: error});
        }
      }
    }));
  }
  armed = true;
}

rpc.exports = {
  configure(config) {
    if (settings || Process.arch !== 'arm64' || config.read_bridge !== 21022348 || config.write_bridge !== 21018548 ||
        !Number.isInteger(config.max_events) || config.max_events < 32 || config.max_events > 4096) {
      throw new Error('explicit bounded read-only PN timeline contract required');
    }
    settings = config;
    moduleObserver = Process.attachModuleObserver({onAdded: arm});
    return true;
  },
  phase(value) { phase = value; return true; },
  snapshot() {
    return {schema: 'e2.1n-original-pn-timeline.v4', armed, calls, events, errors, dropped,
      active: Array.from(active.values()), retained_listeners: listeners.length,
      retained_return_sites: returnSites.size, pending_threads: pending.size,
      module_observer_retained: moduleObserver !== null,
      coverage: 'PN native entry and observed caller return instruction only; Java monitors and scheduler ordering unknown',
      clock: 'wall milliseconds plus script-local total event sequence; not CPU timestamps',
      general_thread_safety: 'unknown', no_vm_verified: false, strict_gate: 'BLOCKED'};
  }
};
