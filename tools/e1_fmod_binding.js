'use strict';

let settings = null;
let hostPhase = 'load';
const events = [];
const errors = [];
const listeners = [];
const calls = new Map();
const watched = new Set();
let installed = false;

function location(address) {
  const module = Process.findModuleByAddress(address);
  return module ? module.name + '+' + address.sub(module.base) : address.toString();
}

function record(kind, fields) {
  events.push(Object.assign({kind, phase: hostPhase, thread: Process.getCurrentThreadId(),
    sequence: events.length}, fields));
}

function symbolName(address) {
  const bytes = [];
  for (let index = 0; index < 256; index++) {
    const value = address.add(index).readU8();
    if (value === 0) return String.fromCharCode(...bytes);
    if (value > 127) throw new Error('non-ASCII resolver symbol');
    bytes.push(value);
  }
  throw new Error('unterminated resolver symbol');
}

function watchResource(target, name, table) {
  const identity = target.toString();
  if (watched.has(identity)) return;
  watched.add(identity);
  listeners.push(Interceptor.attach(target, {
    onEnter(args) {
      const caller = location(this.returnAddress);
      const key = [name, caller, hostPhase, Process.getCurrentThreadId()].join('|');
      let row = calls.get(key);
      if (!row) {
        row = {name, caller, target: location(target), phase: hostPhase,
          thread: Process.getCurrentThreadId(), count: 0, samples: []};
        calls.set(key, row);
      }
      row.count++;
      if (row.samples.length < 3) {
        this.sample = {argument0: args[0].toString(), argument1: args[1].toString(),
          stored_manager: table.readPointer().toString(),
          java_global_ref: table.add(8).readPointer().toString()};
        row.samples.push(this.sample);
      }
    },
    onLeave(result) { if (this.sample) this.sample.result = result.toString(); }
  }));
  Interceptor.flush();
}

rpc.exports = {
  configure(config) {
    if (settings) throw new Error('configuration is immutable');
    if (!['observe', 'redirect'].includes(config.mode)) throw new Error('invalid binding mode');
    settings = config;
    Process.attachModuleObserver({onAdded(module) {
      if (module.name !== config.module) return;
      try {
        const table = module.base.add(config.table_offset);
        const symbols = new Set(config.symbols);
        for (const name of symbols) {
          const target = config.mode === 'redirect'
            ? Process.getModuleByName(config.provider_module).getExportByName(config.provider_symbols[name])
            : Process.getModuleByName('libandroid.so').getExportByName(name);
          watchResource(target, name, table);
        }
        for (const entry of config.resolver_imports) {
          const target = module.base.add(entry.offset).readPointer();
          record('resolver_import', {name: entry.name, slot: location(module.base.add(entry.offset)),
            actual_target: location(target)});
          listeners.push(Interceptor.attach(target, {
            onEnter(args) {
              this.resolution = {name: entry.name, caller: location(this.returnAddress),
                actual_target: location(target), argument0: args[0].toString()};
              try {
                this.resolution.request = symbolName(entry.name === 'dlsym' ? args[1] : args[0]);
              } catch (error) {
                this.resolution.request_error = String(error);
                this.resolution.coverage = 'unknown';
              }
            },
            onLeave(result) {
              record('resolver_call', Object.assign(this.resolution,
                {result: result.toString(), resolved_target: location(result)}));
            }
          }));
        }
        listeners.push(Interceptor.attach(module.base.add(config.resolve_offset), {
          onEnter(args) {
            this.name = null;
            try {
              this.name = symbolName(args[1]);
            } catch (error) {
              errors.push(String(error));
              record('unreadable_resolver_request', {error: String(error),
                caller: location(this.returnAddress), coverage: 'unknown'});
            }
            this.output = args[2];
            this.caller = location(this.returnAddress);
          },
          onLeave(result) {
            if (!symbols.has(this.name)) {
              record('unknown_resolver_request', {name: this.name, caller: this.caller});
              return;
            }
            const target = this.output.readPointer();
            const original = Process.getModuleByName('libandroid.so').getExportByName(this.name);
            const verified = result.toInt32() === 0 && target.equals(original);
            const slot = config.table_slots.find(entry => entry.name === this.name);
            const slotVerified = slot && this.output.equals(table.add(slot.offset));
            record('resolved_binding', {name: this.name, caller: this.caller,
              output_slot: location(this.output), actual_target: location(target),
              verified, slot_verified: Boolean(slotVerified)});
            if (!verified || !slotVerified) {
              errors.push('unexpected binding for ' + this.name);
              return;
            }
            if (config.mode === 'redirect') {
              const replacementName = config.provider_symbols[this.name];
              if (!replacementName) throw new Error('missing provider binding');
              const replacement = Process.getModuleByName(config.provider_module).getExportByName(replacementName);
              this.output.writePointer(replacement);
              watchResource(replacement, this.name, table);
              record('redirected_binding', {name: this.name, output_slot: location(this.output),
                target: location(replacement), verified: this.output.readPointer().equals(replacement)});
            } else watchResource(target, this.name, table);
          }
        }));
        listeners.push(Interceptor.attach(module.base.add(config.initialize_offset), {
          onEnter() {
            record('initialize_enter', {caller: location(this.returnAddress),
              manager_before: table.readPointer().toString()});
          },
          onLeave(result) {
            record('initialize_leave', {result: result.toInt32(),
              manager: table.readPointer().toString(), java_global_ref: table.add(8).readPointer().toString(),
              bindings: config.table_slots.map(entry => ({name: entry.name,
                slot: location(table.add(entry.offset)), target: location(table.add(entry.offset).readPointer())}))});
          }
        }));
        installed = true;
      } catch (error) {
        errors.push(String(error));
        send({event: 'fmod_binding_error', error: String(error)});
      }
    }});
    return true;
  },
  phase(name) { hostPhase = name; },
  snapshot() {
    return {schema: 'e1-fmod-binding.v1', mode: settings.mode, installed, events, errors,
      resource_calls: Array.from(calls.values()),
      coverage: 'configured resolver and initialization boundaries only; other paths unknown'};
  }
};
