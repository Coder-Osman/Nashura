'use strict';

let settings = null;
let helper = null;
let commandMethod = null;
const events = [];
let retired = false;
let allocatedGlobals = 0;
let deletedGlobals = 0;
let pnBridge = null;
let pnLibrary = null;
const cold = {armed: false, attempted: false, installed: false, installing: false,
  entries: 0, returns: 0, reentrant_entries: 0, windows: [], external_windows: 0, errors: []};
const returnSites = new Map();
const pendingWindows = new Map();
let pathTraceStarted = false;
let pathTraceStopped = false;

function beginPathTrace() {
  if (!settings.pn_path_trace || pathTraceStarted) return;
  if (invoke('path-trace-start:' + settings.pn_path_trace) !== 'path-trace-started') throw new Error('path trace start receipt differs');
  pathTraceStarted = true;
}

function watchReturnSite(address) {
  const site = address.toString();
  if (returnSites.has(site)) return;
  const range = Process.findRangeByAddress(address);
  if (!range || !range.protection.includes('x')) throw new Error('return site not executable');
  returnSites.set(site, Interceptor.attach(address, {
    onEnter() {
      const thread = Process.getCurrentThreadId();
      const stack = pendingWindows.get(thread) || [];
      const window = stack[stack.length - 1];
      if (!window || window.return_site !== site) return;
      stack.pop();
      try {
        window.after = invoke('snapshot');
        window.completion = 'original callsite return instruction';
        if (settings.pn_path_trace && window.operation === 'write') {
          if (invoke('path-trace-stop') !== 'path-trace-stopped') throw new Error('path trace stop receipt differs');
          pathTraceStopped = true;
        }
      } catch (failure) { cold.errors.push({stage: 'return-site', error: String(failure)}); }
    }
  }));
}

function invoke(operation) {
  if (retired) throw new Error('observer references already retired');
  const engine = Process.getModuleByName('libg.so');
  const environment = new NativeFunction(engine.base.add(settings.jni_attach), 'pointer', [])();
  if (environment.isNull()) throw new Error('original JNI attach returned null');
  const table = environment.readPointer();
  const jni = (slot, result, types) => new NativeFunction(table.add(slot * Process.pointerSize).readPointer(),
    result, ['pointer', ...types]);
  const pending = jni(228, 'uchar', []);
  const locals = [];
  const checked = result => {
    if (pending(environment)) throw new Error('JNI exception left pending; stop owned attempt');
    return result;
  };
  if (pending(environment)) throw new Error('preexisting JNI exception left pending');
  const local = result => { checked(result); if (result.isNull()) throw new Error('missing JNI object'); locals.push(result); return result; };
  const text = value => local(jni(167, 'pointer', ['pointer'])(environment, Memory.allocUtf8String(value)));
  const find = name => local(jni(6, 'pointer', ['pointer'])(environment, Memory.allocUtf8String(name)));
  const method = (type, name, signature, slot) => {
    const address = checked(jni(slot, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, type,
      Memory.allocUtf8String(name), Memory.allocUtf8String(signature)));
    if (address.isNull()) throw new Error('missing JNI method');
    return address;
  };
  const args = values => {
    const buffer = Memory.alloc(Math.max(8, values.length * 8));
    values.forEach((value, index) => buffer.add(index * 8).writePointer(value));
    return buffer;
  };
  try {
    if (!helper) {
      const threadType = find('android/app/ActivityThread');
      const current = method(threadType, 'currentApplication', '()Landroid/app/Application;', 113);
      const application = local(jni(116, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, threadType, current, ptr(0)));
      const contextType = find('android/content/Context');
      const loaderMethod = method(contextType, 'getClassLoader', '()Ljava/lang/ClassLoader;', 33);
      const parent = local(jni(36, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, application, loaderMethod, ptr(0)));
      const dexType = find('dalvik/system/DexClassLoader');
      const constructor = method(dexType, '<init>', '(Ljava/lang/String;Ljava/lang/String;Ljava/lang/String;Ljava/lang/ClassLoader;)V', 33);
      const loader = local(jni(30, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, dexType, constructor,
        args([text(settings.dex_path), text(settings.optimized_directory), ptr(0), parent])));
      const loaderType = find('java/lang/ClassLoader');
      const loadMethod = method(loaderType, 'loadClass', '(Ljava/lang/String;)Ljava/lang/Class;', 33);
      const type = local(jni(36, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, loader, loadMethod,
        args([text('org.nashura.evidence.E21PreferenceObserver')])));
      helper = checked(jni(21, 'pointer', ['pointer'])(environment, type));
      if (helper.isNull()) throw new Error('global helper class allocation failed');
      allocatedGlobals++;
      commandMethod = method(type, 'command', '(Ljava/lang/String;)Ljava/lang/String;', 113);
    }
    const output = local(jni(116, 'pointer', ['pointer', 'pointer', 'pointer'])(environment, helper, commandMethod,
      args([text(operation)])));
    const characters = checked(jni(169, 'pointer', ['pointer', 'pointer'])(environment, output, ptr(0)));
    if (characters.isNull()) throw new Error('output string unavailable');
    let result;
    try { result = characters.readUtf8String(); }
    finally { jni(170, 'void', ['pointer', 'pointer'])(environment, output, characters); }
    events.push({operation, status: 'returned', thread: Process.getCurrentThreadId()});
    if (operation === 'release') {
      checked(jni(22, 'void', ['pointer'])(environment, helper));
      helper = null;
      commandMethod = null;
      deletedGlobals++;
      retired = true;
    }
    return result;
  } finally {
    for (const reference of locals.reverse()) jni(23, 'void', ['pointer'])(environment, reference);
  }
}

function armCold(module) {
  if (module.name !== 'libg.so' || cold.armed) return;
  if (settings.pn_bridge) {
    cold.armed = true;
    pnBridge = createPNBridge(settings.pn_bridge, invoke, {
      entry(writing) {
        cold.entries++;
        if (cold.installing) {
          cold.reentrant_entries++;
          throw new Error('reentrant original bridge during cold backend installation');
        }
        if (!cold.attempted) {
          cold.attempted = true;
          cold.installing = true;
          cold.install_entry = {operation: writing ? 'write' : 'read', offset: writing ? 21018548 : 21022348,
            thread: Process.getCurrentThreadId(), entry: cold.entries};
          try {
            cold.install_result = invoke('install');
            cold.installed = cold.install_result === 'installed\t2';
            if (!cold.installed) throw new Error('unexpected cold backend install receipt');
          } catch (failure) {
            cold.errors.push({stage: 'install', error: String(failure)});
            throw failure;
          } finally { cold.installing = false; }
        }
        if (!cold.installed) throw new Error('cold PN backend not installed');
      },
      pn(row) {
        if (row.caller === 'external-probe') { cold.external_windows++; return; }
        if (cold.windows.length >= 32) throw new Error('cold PN window budget exhausted');
        const address = ptr(row.return_site);
        watchReturnSite(address);
        const window = {operation: row.operation, thread: row.thread, entry: cold.entries,
          caller: address.sub(module.base).toString(), return_site: row.return_site,
          module_base: row.module_base, bridge_event: pnBridge.snapshot().events.length - 1,
          before: invoke('snapshot')};
        cold.windows.push(window);
        beginPathTrace();
        const stack = pendingWindows.get(row.thread) || [];
        stack.push(window);
        pendingWindows.set(row.thread, stack);
      }
    }, pnLibrary);
    return;
  }
  for (const [operation, offset] of [['read', 21022348], ['write', 21018548]]) {
    const target = module.base.add(offset);
    const range = Process.findRangeByAddress(target);
    if (!range || !range.protection.includes('x')) throw new Error('cold observer target not executable');
    Interceptor.attach(target, {
      onEnter(args) {
        cold.entries++;
        if (cold.installing) { cold.reentrant_entries++; return; }
        if (!cold.attempted) {
          cold.attempted = true;
          cold.installing = true;
          cold.install_entry = {operation, offset, thread: Process.getCurrentThreadId(), entry: cold.entries};
          try {
            cold.install_result = invoke('install');
            cold.installed = cold.install_result === 'installed\t2';
            if (!cold.installed) throw new Error('unexpected cold install receipt');
          } catch (failure) {
            cold.errors.push({stage: 'install', error: String(failure)});
          } finally { cold.installing = false; }
        }
        if (!cold.installed || retired) return;
        try {
          if (args[0].add(4).readU32() !== 8) return;
          const data = args[0].add(8).readPointer().and('0x00ffffffffffffff');
          if (data.isNull() || data.readUtf8String(8) !== 'PN_POPUP' || data.add(8).readU8() !== 0) return;
          if (this.returnAddress.compare(module.base) < 0 || this.returnAddress.compare(module.base.add(module.size)) >= 0) {
            cold.external_windows++;
            return;
          }
          if (cold.windows.length >= 32) throw new Error('cold PN window budget exhausted');
          watchReturnSite(this.returnAddress);
          const window = {operation, thread: Process.getCurrentThreadId(), entry: cold.entries,
            caller: this.returnAddress.sub(module.base).toString(), return_site: this.returnAddress.toString(),
            before: invoke('snapshot')};
          cold.windows.push(window);
          beginPathTrace();
          const stack = pendingWindows.get(window.thread) || [];
          stack.push(window);
          pendingWindows.set(window.thread, stack);
        } catch (failure) { cold.errors.push({stage: 'entry', error: String(failure)}); }
      },
      onLeave() {
        cold.returns++;
      }
    });
  }
  cold.armed = true;
}

rpc.exports = {
  bridgeInstall(config) {
    if (!settings || settings.enable_pn_backend !== true || pnBridge || retired ||
        settings.install_at !== 'post-cold-ready') throw new Error('explicit post-cold-ready PN backend required');
    pnBridge = createPNBridge(config, invoke);
    return pnBridge.snapshot();
  },
  bridgeRetire() {
    if (!pnBridge) throw new Error('PN bridge missing');
    return pnBridge.retire();
  },
  bridgeSnapshot() {
    if (!pnBridge) throw new Error('PN bridge missing');
    return pnBridge.snapshot();
  },
  phase(value) { if (pnBridge) pnBridge.phase(value); return true; },
  configure(config) {
    if (settings || config.jni_attach !== 0x17d8ae8 ||
        !config.dex_path.startsWith('/data/user/0/nullsroyale.rel.free/') || !config.dex_path.endsWith('.dex') ||
        !config.optimized_directory.startsWith('/data/user/0/nullsroyale.rel.free/')) {
      throw new Error('invalid immutable return observer configuration');
    }
    if (config.install_at !== undefined && !['post-cold-ready', 'first-native-bridge'].includes(config.install_at)) {
      throw new Error('unsupported observer installation phase');
    }
    if (config.pn_bridge && (config.install_at !== 'first-native-bridge' || config.enable_pn_backend !== true ||
        config.pn_bridge.install_at !== 'first-native-bridge')) throw new Error('explicit cold PN backend required');
    if (config.pn_path_trace && (config.install_at !== 'first-native-bridge' ||
        !/^\/data\/user\/0\/nullsroyale[.]rel[.]free\/e21k-[a-z0-9-]+[.]trace$/.test(config.pn_path_trace))) {
      throw new Error('explicit natural path trace required');
    }
    settings = config;
    if (config.pn_bridge) pnLibrary = Module.load(config.pn_bridge.library);
    if (config.install_at === 'first-native-bridge') Process.attachModuleObserver({onAdded: armCold});
    return true;
  },
  command(operation) {
    const matrix = settings && settings.enable_pn_matrix === true &&
      (/^pn-set:[A-Za-z0-9+/=]*:[A-Za-z0-9+/=]*$/.test(operation) ||
        ['pn-begin', 'pn-state', 'pn-restore'].includes(operation));
    const codec = settings && settings.enable_pn_codec === true &&
      (['codec-info', 'codec-runtime'].includes(operation) || /^codec-(read|state|inspect):[01]$/.test(operation) ||
       /^codec-fixture:[01]:[A-Za-z0-9+/=]*$/.test(operation) ||
       /^codec-decrypt:[01]:[A-Za-z0-9+/=]*$/.test(operation) ||
       /^codec-raw:[01]:[A-Za-z0-9+/=]*$/.test(operation) ||
       /^codec-encode:[01]:(key|value):[A-Za-z0-9+/=]*$/.test(operation));
    const derivation = settings && settings.enable_pn_derivation === true && operation === 'derivation-source';
    if (pnBridge && pnBridge.snapshot().installed && ['uninstall', 'release'].includes(operation)) throw new Error('retire PN bridge before backend');
    if (!settings || (!matrix && !codec && !derivation && !['install', 'snapshot', 'uninstall', 'files', 'read-stores', 'release'].includes(operation))) throw new Error('invalid command');
    if (operation === 'install' && settings.install_at === 'first-native-bridge') throw new Error('cold install is driven by original bridge only');
    return invoke(operation);
  },
  snapshot() {
    return {events, cold, path_trace: {started: pathTraceStarted, stopped: pathTraceStopped},
      references: {allocated_globals: allocatedGlobals, deleted_globals: deletedGlobals, retired},
      scope: settings && settings.install_at === 'first-native-bridge'
        ? (settings.pn_bridge ? 'cold PN replacement at original callsites; original Java persistence retained'
          : 'original libg PN_POPUP bridge callsites after first native bridge; earlier Java accesses and external direct probes unobserved')
        : 'two temporary forwarding SharedPreferences wrappers; explicit post-cold-ready windows only',
      coverage: 'actual delegated return values only; no JVMTI or exhaustive Java coverage', no_vm_verified: false};
  }
};
