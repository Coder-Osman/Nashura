'use strict';

let receipt = null;
let provider = null;
let metrics = null;

rpc.exports = {
  configure(config) {
    if (receipt) throw new Error('configuration is immutable');
    provider = Module.load(config.provider_path);
    const initialize = new NativeFunction(provider.getExportByName('e1_initialize'),
      'pointer', ['pointer', 'pointer', 'pointer']);
    const context = initialize(...[config.apk_path, config.index_path, config.audit_path]
      .map(value => Memory.allocUtf8String(value)));
    if (context.isNull()) throw new Error('provider initialization failed; no redirects installed');
    metrics = new NativeFunction(provider.getExportByName('e1_metric'), 'uint64', ['uint']);
    receipt = {schema: 'e1-resource-redirect.v1', installed: false, redirects: [], errors: [],
      resolver_observations: [], resolver_coverage: 'not installed',
      scope: 'engine import slots only; VM and configuration JNI unchanged'};
    try {
      Interceptor.attach(Process.getModuleByName('libdl.so').getExportByName('dlsym'), {
        onEnter(args) {
          this.resourceName = null;
          try {
            const name = args[1].readUtf8String(256);
            if (name.startsWith('AAsset')) {
              this.resourceName = name;
              const caller = Process.findModuleByAddress(this.returnAddress);
              this.callerModule = caller ? caller.name : 'unknown';
            }
          } catch (_) {}
        },
        onLeave(result) {
          if (!this.resourceName) return;
          const target = Process.findModuleByAddress(result);
          receipt.resolver_observations.push({name: this.resourceName, caller: this.callerModule,
            resolved_module: target ? target.name : 'unknown', resolved: !result.isNull()});
        }
      });
      receipt.resolver_coverage = 'libdl dlsym observed; other resolver paths unknown';
    } catch (error) {
      receipt.errors.push('resolver observation failed: ' + String(error));
    }
    Process.attachModuleObserver({onAdded(module) {
      if (module.name !== config.engine_module) return;
      try {
        const slots = config.imports.map(entry => {
          if (!entry.name.startsWith('AAsset')) throw new Error('non-resource import rejected');
          const address = module.base.add(entry.offset);
          const original = Process.getModuleByName('libandroid.so').getExportByName(entry.name);
          if (!address.readPointer().equals(original)) throw new Error('unexpected resource import identity');
          const range = Process.findRangeByAddress(address);
          if (!range) throw new Error('missing import mapping');
          return {entry, address, original, protection: range.protection,
            replacement: provider.getExportByName('e1_' + entry.name)};
        });
        for (const slot of slots) {
          if (!Memory.protect(slot.address, Process.pointerSize, 'rw-')) throw new Error('import protection failed');
          slot.address.writePointer(slot.replacement);
          if (!Memory.protect(slot.address, Process.pointerSize, slot.protection)) throw new Error('import protection restore failed');
          receipt.redirects.push({name: slot.entry.name, verified: slot.address.readPointer().equals(slot.replacement)});
        }
        receipt.installed = receipt.redirects.length === config.imports.length;
      } catch (error) {
        receipt.errors.push(String(error));
        send({event: 'resource_redirect_failed', error: String(error)});
      }
    }});
    return true;
  },
  snapshot() {
    return {receipt, metrics: metrics ? {calls: metrics(0).toString(), failures: metrics(1).toString(),
      live_assets: metrics(2).toString(), entries: metrics(3).toString()} : null};
  }
};
