'use strict';

const events = [], coverage = [], errors = [];
let lost = 0;
let nextCall = 0;
let includeBatchCounts = false;
let includeBatchPhases = false;
const scopes = new Map();
const batchScopes = new Map();
const finishScopes = new Map();
const lookupScopes = new Map();
const innerScopes = new Map();
const insertionHistory = new Map();
const populationHistory = new Map();
const allocationHistory = new Map();
const pathCreationHistory = new Map();
const pathSourceHistory = new Map();
const sourceLoaderScopes = new Map();
const sourceParentHistory = new Map();
const sourceDbHistory = new Map();
const dbCtorScopes = new Map();
const dbInitScopes = new Map();
const dbConstructors = new Map();
const dbFactoryScopes = new Map();
const engineDbScopes = new Map();
let engineDbOwner = null;
const engineOwnerConstructors = new Map();
const engineOwnerConstructorScopes = new Map();
const engineOwnerGetterScopes = new Map();
const engineOwnerGetterResults = new Map();
const engineOwnerGetterHolders = new Map();
const engineOwnerHolderSetups = new Map();
const engineOwnerHolderFactories = new Map();
const engineOwnerInitScopes = new Map();
const observedSlots = [9, 10, 22, 34, 35, 123, 133, 166, 173, 179, 187, 189, 191, 205, 212, 213, 214];
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
function nativeString16(p) {
  try {
    const object = p.and(ptr('0x00ffffffffffffff'));
    const length = object.add(4).readS32();
    if (length < 0 || length > 1024) return null;
    const data = length < 8 ? object.add(8) : object.add(8).readPointer().and(ptr('0x00ffffffffffffff'));
    const value = boundedString(data);
    return value !== null && value.length === length ? value : null;
  } catch (_) { return null; }
}
function lookupInput(p) {
  function readPointer(address) {
    const clean = address.and(ptr('0x00ffffffffffffff'));
    const range = Process.findRangeByAddress(clean);
    if (clean.isNull() || !range || !range.protection.includes('r') ||
        clean.add(8).compare(range.base.add(range.size)) > 0) throw new Error('lookup input unreadable');
    return clean.readPointer().and(ptr('0x00ffffffffffffff'));
  }
  const first = readPointer(p);
  return {first_nonnull: !first.isNull(), nested_nonnull: first.isNull() ? null : !readPointer(first.add(8)).isNull()};
}
function tableCounts(context) {
  function readable(value, size) {
    const address = value.and(ptr('0x00ffffffffffffff'));
    const range = Process.findRangeByAddress(address);
    if (address.isNull() || !range || !range.protection.includes('r') ||
        address.add(size).compare(range.base.add(range.size)) > 0) throw new Error('unreadable catalog memory');
    return address;
  }
  const root = readable(context, 219 * 8);
  return observedSlots.map(slot => {
    const object = root.add(slot * 8).readPointer();
    if (object.isNull()) return [slot, -1];
    const table = readable(object, 0x90);
    const capacity = table.add(0x38).readS32(), count = table.add(0x3c).readS32();
    const owner = table.add(0x78).readPointer().and(ptr('0x00ffffffffffffff'));
    const kind = table.add(0x84).readS32();
    if (!owner.equals(root) || kind !== slot || count < 0 || capacity < count || capacity > 1000000)
      throw new Error('catalog table invariant failed');
    return [slot, count];
  });
}
function attach(module, offset, label, callbacks) {
  try {
    const address = module.base.add(offset);
    if (!Process.findRangeByAddress(address).protection.includes('x')) throw new Error('not executable');
    Interceptor.attach(address, callbacks);
    coverage.push({label: label, installed: true});
  } catch (error) { coverage.push({label: label, installed: false}); errors.push(label + ': ' + String(error)); }
}
function collectPath(module, history, args, returnAddress) {
  const mask = ptr('0x00ffffffffffffff');
  const owner = args[0].and(mask).readPointer().and(mask);
  if (owner.isNull()) throw new Error('null path owner');
  const key = owner.toString();
  if (!history.has(key)) {
    if (history.size >= 8192) throw new Error('path owner limit');
    history.set(key, new Map());
  }
  const items = history.get(key);
  const string = args[1].and(mask);
  const header = string.readU8();
  const length = (header & 1) ? string.add(8).readU64().toNumber() : header >>> 1;
  if (length > 4096) throw new Error('path length limit');
  const bytes = (header & 1) ? string.add(16).readPointer().and(mask) : string.add(1);
  const data = new Uint8Array(bytes.readByteArray(length + 1));
  if (data.length !== length + 1 || data[length] !== 0) throw new Error('path terminator missing');
  const caller = returnAddress.and(mask);
  const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
    ? caller.sub(module.base).toString() : 'external';
  const hex = Array.from(data.slice(0, length), byte => byte.toString(16).padStart(2, '0')).join('');
  const identity = label + ':' + hex;
  if (!items.has(identity)) {
    if (items.size >= 32768) throw new Error('unique path history limit');
    items.set(identity, {caller: label, bytes_hex: hex, calls: 0, returns: 0});
  }
  const item = items.get(identity);
  item.calls++;
  return item;
}
function observe(module) {
  if (module.name !== 'libg.so') return;
  if (includeBatchPhases) {
    attach(module, 0x13a3d14, 'owner_holder_factory_candidate', {
      onEnter: function (args) {
        this.allocator = args[0].and(ptr('0x00ffffffffffffff'));
        this.output = this.context.x8.and(ptr('0x00ffffffffffffff'));
        this.factoryId = ++nextCall;
        this.factoryThread = Process.getCurrentThreadId();
      },
      onLeave: function () {
        try {
          const holder = this.output.readPointer().and(ptr('0x00ffffffffffffff'));
          const allocator = holder.add(8).readPointer().and(ptr('0x00ffffffffffffff'));
          const table = this.allocator.readPointer().and(ptr('0x00ffffffffffffff'));
          const tableModule = Process.findModuleByAddress(table);
          const tableOffset = tableModule && tableModule.name === module.name ? table.sub(module.base).toString() : null;
          engineOwnerHolderFactories.set(holder.toString(), {id: this.factoryId, allocator: this.allocator});
          record({kind: 'owner_holder_factory_return', id: this.factoryId, holder_nonnull: !holder.isNull(),
                  allocator_nonnull: !this.allocator.isNull(), allocator_matches_holder: allocator.equals(this.allocator),
                  allocator_vtable_module: tableModule ? tableModule.name : null, allocator_vtable_offset: tableOffset});
        } catch (error) { errors.push('owner holder factory observation: ' + String(error)); }
      }
    });
    attach(module, 0x13a3e3c, 'owner_holder_setup_candidate', {
      onEnter: function (args) {
        this.holder = args[0].and(ptr('0x00ffffffffffffff'));
        this.holderThread = Process.getCurrentThreadId();
        this.setupId = ++nextCall;
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const factory = engineOwnerHolderFactories.get(this.holder.toString()) || null;
        const allocator = this.holder.add(8).readPointer().and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        record({kind: 'owner_holder_setup_enter', id: this.setupId, caller: label,
                factory_id: factory && factory.allocator.equals(allocator) ? factory.id : null,
                slot_before_nonnull: !this.holder.add(0x20).readPointer().isNull()});
      },
      onLeave: function () {
        const field = this.holder.add(0x20).readPointer().and(ptr('0x00ffffffffffffff'));
        engineOwnerHolderSetups.set(this.holder.toString(), {id: this.setupId, field: field, thread: this.holderThread});
        record({kind: 'owner_holder_setup_return', id: this.setupId, slot_after_nonnull: !field.isNull()});
      }
    });
    attach(module, 0x7557d0, 'engine_owner_getter_candidate', {
      onEnter: function () {
        this.ownerThread = Process.getCurrentThreadId();
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        this.getter = {id: ++nextCall, caller: label, thread: this.ownerThread};
        this.getterHolder = this.context.x19.and(ptr('0x00ffffffffffffff'));
        const stack = engineOwnerGetterScopes.get(this.ownerThread) || [];
        stack.push(this.getter);
        engineOwnerGetterScopes.set(this.ownerThread, stack);
      },
      onLeave: function (retval) {
        const stack = engineOwnerGetterScopes.get(this.ownerThread);
        if (!stack || stack.pop() !== this.getter) errors.push('owner getter scope mismatch');
        if (stack && stack.length === 0) engineOwnerGetterScopes.delete(this.ownerThread);
        const result = retval.and(ptr('0x00ffffffffffffff'));
        if (!result.isNull()) {
          engineOwnerGetterResults.set(result.toString(), {...this.getter});
          engineOwnerGetterHolders.set(result.toString(), this.getterHolder);
        }
      }
    });
    attach(module, 0x755a98, 'engine_owner_constructor_candidate', {
      onEnter: function (args) {
        this.owner = args[0].and(ptr('0x00ffffffffffffff'));
        this.ownerKey = this.owner.toString();
        this.ownerThread = Process.getCurrentThreadId();
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        const previous = engineOwnerConstructors.get(this.ownerKey);
        const stack = engineOwnerGetterScopes.get(this.ownerThread) || [];
        const row = {id: ++nextCall, caller: label, thread: this.ownerThread, returned: false,
                     vtable_matches: null, prior_address_observations: previous ? previous.prior_address_observations + 1 : 0,
                     getter_parent: stack.length ? {...stack[stack.length - 1]} : null};
        engineOwnerConstructors.set(this.ownerKey, row);
        this.ownerRow = row;
        const constructorStack = engineOwnerConstructorScopes.get(this.ownerThread) || [];
        constructorStack.push(row.id);
        engineOwnerConstructorScopes.set(this.ownerThread, constructorStack);
      },
      onLeave: function () {
        this.ownerRow.returned = true;
        try {
          this.ownerRow.vtable_matches = this.owner.readPointer().and(ptr('0x00ffffffffffffff'))
            .equals(module.base.add(0x17ea568));
        } catch (e) {
          errors.push('owner constructor vtable read failed: ' + e);
        }
        const stack = engineOwnerConstructorScopes.get(this.ownerThread);
        if (!stack || stack.pop() !== this.ownerRow.id) errors.push('owner constructor scope mismatch');
        if (stack && stack.length === 0) engineOwnerConstructorScopes.delete(this.ownerThread);
      }
    });
    attach(module, 0x140cc8c, 'app_version_wrapper_candidate', {
      onEnter: function () {
        const thread = Process.getCurrentThreadId();
        const stack = engineOwnerConstructorScopes.get(thread) || [];
        if (!stack.length) return;
        this.ownerParent = stack[stack.length - 1];
        this.output = this.context.x8.and(ptr('0x00ffffffffffffff'));
      },
      onLeave: function () {
        if (!this.ownerParent) return;
        const value = nativeString16(this.output);
        if (value === null) errors.push('app version result unreadable');
        record({kind: 'app_version_wrapper', owner_constructor_id: this.ownerParent, value: value});
      }
    });
    attach(module, 0x756be8, 'engine_owner_init_candidate', {
      onEnter: function (args) {
        this.initThread = Process.getCurrentThreadId();
        this.initOwner = args[0].and(ptr('0x00ffffffffffffff'));
        this.initArgument = args[1].and(ptr('0x00ffffffffffffff'));
        this.initId = ++nextCall;
        const ctor = engineOwnerConstructors.get(this.initOwner.toString()) || null;
        const getterHolder = engineOwnerGetterHolders.get(this.initOwner.toString()) || ptr(0);
        const holderSetup = engineOwnerHolderSetups.get(getterHolder.toString()) || null;
        const holderRange = getterHolder.isNull() ? null : Process.findRangeByAddress(getterHolder);
        let argumentMatchesGetterHolderField = null;
        if (holderRange && holderRange.protection.includes('r') &&
            getterHolder.add(0x28).compare(holderRange.base.add(holderRange.size)) <= 0) {
          argumentMatchesGetterHolderField = getterHolder.add(0x20).readPointer().and(ptr('0x00ffffffffffffff'))
            .equals(this.initArgument);
        }
        const previousSlot = this.initOwner.add(8).readPointer().and(ptr('0x00ffffffffffffff'));
        const argumentRange = this.initArgument.isNull() ? null : Process.findRangeByAddress(this.initArgument);
        let argumentVtable = null;
        let argumentFirstWordMapped = null, argumentFirstWordModule = null;
        if (argumentRange && argumentRange.protection.includes('r') &&
            this.initArgument.add(8).compare(argumentRange.base.add(argumentRange.size)) <= 0) {
          const table = this.initArgument.readPointer().and(ptr('0x00ffffffffffffff'));
          const ownerModule = Process.findModuleByAddress(table);
          argumentVtable = ownerModule && ownerModule.name === module.name ? table.sub(module.base).toString() : 'external';
          argumentFirstWordMapped = !table.isNull() && Process.findRangeByAddress(table) !== null;
          argumentFirstWordModule = ownerModule ? ownerModule.name : null;
        }
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        if (engineOwnerInitScopes.has(this.initThread)) errors.push('nested owner initialization');
        engineOwnerInitScopes.set(this.initThread, {id: this.initId, owner: this.initOwner});
        record({kind: 'engine_owner_init_enter', id: this.initId, caller: label,
                owner_constructor_id: ctor ? ctor.id : null, argument_nonnull: !this.initArgument.isNull(),
                argument_vtable: argumentVtable, argument_readable: !!argumentRange && argumentRange.protection.includes('r'),
                argument_first_word_mapped: argumentFirstWordMapped, argument_first_word_module: argumentFirstWordModule,
                argument_matches_getter_holder_field: argumentMatchesGetterHolderField,
                holder_setup_id: holderSetup && holderSetup.field.equals(this.initArgument) ? holderSetup.id : null,
                argument_matches_slot_before: previousSlot.equals(this.initArgument)});
      },
      onLeave: function () {
        const current = engineOwnerInitScopes.get(this.initThread);
        if (!current || current.id !== this.initId) errors.push('owner initialization scope mismatch');
        engineOwnerInitScopes.delete(this.initThread);
        record({kind: 'engine_owner_init_return', id: this.initId,
                argument_matches_slot_after: this.initOwner.add(8).readPointer().and(ptr('0x00ffffffffffffff'))
                  .equals(this.initArgument)});
      }
    });
    attach(module, 0x75760c, 'engine_db_init_candidate', {
      onEnter: function (args) {
        this.dbThread = Process.getCurrentThreadId();
        this.owner = args[0].and(ptr('0x00ffffffffffffff'));
        engineDbOwner = this.owner;
        this.call = ++nextCall;
        const root = module.base.add(0x19ad878).readPointer().and(ptr('0x00ffffffffffffff'));
        const context = root.isNull() ? ptr(0) : root.add(0x20).readPointer().and(ptr('0x00ffffffffffffff'));
        const table = this.owner.readPointer().and(ptr('0x00ffffffffffffff'));
        const tableModule = Process.findModuleByAddress(table);
        const vtable = tableModule && tableModule.name === module.name ? table.sub(module.base).toString() : 'external';
        const ctor = engineOwnerConstructors.get(this.owner.toString()) || null;
        const getter = engineOwnerGetterResults.get(this.owner.toString()) || null;
        const init = engineOwnerInitScopes.get(this.dbThread) || null;
        const references = {};
        for (const [name, address] of [['root', root], ['context', context]]) {
          const range = address.isNull() ? null : Process.findRangeByAddress(address);
          if (!range || !range.protection.includes('r') || address.add(0x1000).compare(range.base.add(range.size)) > 0) {
            references[name] = null;
          } else {
            references[name] = Memory.scanSync(address, 0x1000, this.owner.toMatchPattern())
              .filter(hit => hit.address.sub(address).toInt32() % 8 === 0)
              .map(hit => hit.address.sub(address).toString());
          }
        }
        references.globals = [];
        for (const range of module.enumerateRanges('rw-')) {
          const hits = Memory.scanSync(range.base, range.size, this.owner.toMatchPattern());
          references.globals.push(...hits.filter(hit => hit.address.sub(module.base).toInt32() % 8 === 0)
            .map(hit => hit.address.sub(module.base).toString()));
        }
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        if (engineDbScopes.has(this.dbThread)) errors.push('nested engine database init');
        engineDbScopes.set(this.dbThread, this.call);
        record({kind: 'engine_db_init_enter', id: this.call, caller: label,
                owner_is_root: this.owner.equals(root), owner_is_context: this.owner.equals(context),
                root_nonnull: !root.isNull(), context_nonnull: !context.isNull(),
                owner_vtable: vtable, owner_references: references, owner_constructor: ctor ? {...ctor} : null,
                owner_getter: getter ? {...getter} : null,
                owner_init_id: init && init.owner.equals(this.owner) ? init.id : null,
                slot_before_nonnull: !this.owner.add(0x3f0).readPointer().isNull()});
      },
      onLeave: function () {
        record({kind: 'engine_db_init_return', id: this.call,
                slot_after_nonnull: !this.owner.add(0x3f0).readPointer().isNull()});
        if (engineDbScopes.get(this.dbThread) !== this.call) errors.push('engine database init scope mismatch');
        engineDbScopes.delete(this.dbThread);
      }
    });
    attach(module, 0x11b4b30, 'database_factory_candidate', {
      onEnter: function (args) {
        this.dbThread = Process.getCurrentThreadId();
        this.dbPath = boundedString(args[1].and(ptr('0x00ffffffffffffff')));
        if (this.dbPath !== 'assets.scdb') return;
        this.dbCall = ++nextCall;
        this.engineParent = engineDbScopes.get(this.dbThread) || null;
        this.dbOutput = this.context.x8.and(ptr('0x00ffffffffffffff'));
        this.dbCaller = this.returnAddress.sub(module.base).toString();
        if (dbFactoryScopes.has(this.dbThread)) errors.push('nested database factory');
        dbFactoryScopes.set(this.dbThread, this.dbCall);
      },
      onLeave: function () {
        if (!this.dbCall) return;
        try { record({kind: 'database_factory', id: this.dbCall, caller: this.dbCaller, engine_parent: this.engineParent,
                      result_nonnull: !this.dbOutput.readPointer().isNull()}); }
        catch (error) { errors.push('database factory output: ' + String(error)); }
        if (dbFactoryScopes.get(this.dbThread) !== this.dbCall) errors.push('database factory scope mismatch');
        dbFactoryScopes.delete(this.dbThread);
      }
    });
    attach(module, 0x11b4df4, 'database_constructor_candidate', {
      onEnter: function (args) {
        this.observerDbThread = Process.getCurrentThreadId();
        this.object = args[0].and(ptr('0x00ffffffffffffff'));
        this.objectKey = this.object.toString();
        this.callId = ++nextCall;
        this.path = engineString(args[2]);
        this.factoryParent = dbFactoryScopes.get(this.observerDbThread) || null;
        if (dbCtorScopes.has(this.observerDbThread)) errors.push('nested database constructor');
        dbCtorScopes.set(this.observerDbThread, this.callId);
      },
      onLeave: function () {
        try {
          const state = {id: this.callId, path: this.path, factory_parent: this.factoryParent,
                         reader_nonnull: !this.object.add(0x70).readPointer().isNull()};
          dbConstructors.set(this.objectKey, state);
          record({kind: 'database_constructor', ...state});
        } catch (error) { errors.push('database constructor: ' + String(error)); }
        if (dbCtorScopes.get(this.observerDbThread) !== this.callId) errors.push('database constructor scope mismatch');
        dbCtorScopes.delete(this.observerDbThread);
      }
    });
    attach(module, 0x131a5f0, 'database_open_candidate', {
      onEnter: function (args) {
        this.parent = dbCtorScopes.get(Process.getCurrentThreadId()) || null;
        if (this.parent) {
          this.path = boundedString(args[0].and(ptr('0x00ffffffffffffff')));
          this.output = this.context.x8.and(ptr('0x00ffffffffffffff'));
        }
      },
      onLeave: function () {
        if (!this.parent) return;
        try { record({kind: 'database_open', parent: this.parent, path: this.path,
                      result_nonnull: !this.output.readPointer().isNull()}); }
        catch (error) { errors.push('database open output: ' + String(error)); }
      }
    });
    attach(module, 0x11b4d0c, 'database_init_candidate', {
      onEnter: function (args) {
        this.observerDbThread = Process.getCurrentThreadId();
        this.objectKey = args[0].and(ptr('0x00ffffffffffffff')).toString();
        this.callId = ++nextCall;
        this.dbConstructor = dbConstructors.get(this.objectKey) || null;
        if (dbInitScopes.has(this.observerDbThread)) errors.push('nested database init');
        dbInitScopes.set(this.observerDbThread, this.callId);
        record({kind: 'database_init_enter', id: this.callId,
                constructor_id: this.dbConstructor ? this.dbConstructor.id : null});
      },
      onLeave: function (value) {
        record({kind: 'database_init_return', id: this.callId, result: value.toUInt32()});
        if (dbInitScopes.get(this.observerDbThread) !== this.callId) errors.push('database init scope mismatch');
        dbInitScopes.delete(this.observerDbThread);
      }
    });
    attach(module, 0x11b4248, 'source_loader_candidate', {
      onEnter: function () {
        this.observerThread = Process.getCurrentThreadId();
        if (sourceLoaderScopes.has(this.observerThread)) errors.push('nested source loader');
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        this.label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        sourceLoaderScopes.set(this.observerThread, {label: this.label,
          init: dbInitScopes.get(this.observerThread) || null});
      },
      onLeave: function () {
        if (!sourceLoaderScopes.has(this.observerThread) || sourceLoaderScopes.get(this.observerThread).label !== this.label)
          errors.push('source loader scope mismatch');
        sourceLoaderScopes.delete(this.observerThread);
      }
    });
    attach(module, 0x11b0820, 'path_source_candidate', {
      onEnter: function (args) {
        try {
          this.pathItem = collectPath(module, pathSourceHistory, args, this.returnAddress);
          const owner = args[0].and(ptr('0x00ffffffffffffff')).readPointer().and(ptr('0x00ffffffffffffff')).toString();
          const scope = sourceLoaderScopes.get(Process.getCurrentThreadId());
          const parent = scope ? scope.label : 'unobserved';
          if (!sourceParentHistory.has(owner)) sourceParentHistory.set(owner, {});
          const counts = sourceParentHistory.get(owner);
          counts[parent] = (counts[parent] || 0) + 1;
          if (!sourceDbHistory.has(owner)) sourceDbHistory.set(owner, {});
          const dbCounts = sourceDbHistory.get(owner);
          const init = scope && scope.init ? String(scope.init) : 'unobserved';
          dbCounts[init] = (dbCounts[init] || 0) + 1;
        }
        catch (error) { errors.push('path source: ' + String(error)); }
      },
      onLeave: function () { if (this.pathItem) this.pathItem.returns++; }
    });
    attach(module, 0x11afc60, 'path_creation_candidate', {
      onEnter: function (args) {
        try { this.pathItem = collectPath(module, pathCreationHistory, args, this.returnAddress); }
        catch (error) { errors.push('path creation: ' + String(error)); }
      },
      onLeave: function () { if (this.pathItem) this.pathItem.returns++; }
    });
    attach(module, 0x11b0084, 'tree_node_candidate', {
      onEnter: function (args) {
        const key = args[0].and(ptr('0x00ffffffffffffff')).toString();
        if (!allocationHistory.has(key)) {
          if (allocationHistory.size >= 8192) { errors.push('allocation history limit'); return; }
          allocationHistory.set(key, {calls: 0, returns: 0, callers: {}});
        }
        this.allocation = allocationHistory.get(key);
        this.allocation.calls++;
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        this.allocation.callers[label] = (this.allocation.callers[label] || 0) + 1;
      },
      onLeave: function () { if (this.allocation) this.allocation.returns++; }
    });
    attach(module, 0x11aebb0, 'tree_population_candidate', {
      onEnter: function (args) {
        const key = args[0].and(ptr('0x00ffffffffffffff')).toString();
        if (!populationHistory.has(key)) {
          if (populationHistory.size >= 8192) { errors.push('population history limit'); return; }
          populationHistory.set(key, {calls: 0, returns: 0, callers: {}});
        }
        this.population = populationHistory.get(key);
        this.population.calls++;
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        this.population.callers[label] = (this.population.callers[label] || 0) + 1;
      },
      onLeave: function () { if (this.population) this.population.returns++; }
    });
    attach(module, 0x11b2250, 'tree_insert_candidate', {
      onEnter: function (args) {
        const key = args[0].and(ptr('0x00ffffffffffffff')).toString();
        if (!insertionHistory.has(key)) {
          if (insertionHistory.size >= 8192) { errors.push('insertion history limit'); return; }
          insertionHistory.set(key, {calls: 0, returns: 0, callers: {}});
        }
        this.insertion = insertionHistory.get(key);
        this.insertion.calls++;
        const caller = this.returnAddress.and(ptr('0x00ffffffffffffff'));
        const label = caller.compare(module.base) >= 0 && caller.compare(module.base.add(module.size)) < 0
          ? caller.sub(module.base).toString() : 'external';
        this.insertion.callers[label] = (this.insertion.callers[label] || 0) + 1;
      },
      onLeave: function () { if (this.insertion) this.insertion.returns++; }
    });
  }
  let getterHooksAttempted = false;
  attach(module, 0xe1bdcc, 'table_batch', {
    onEnter: function (args) {
      if (!getterHooksAttempted) {
        getterHooksAttempted = true;
        for (const [label, offset] of [['entry_type', 0xdcb6fc], ['entry_id', 0xdcb474]]) {
          attach(module, offset, label, {
            onEnter: function () { this.batch = batchScopes.get(Process.getCurrentThreadId()) || null; },
            onLeave: function (value) { if (this.batch) record({kind: label, batch: this.batch, value: value.toUInt32()}); }
          });
        }
        if (includeBatchPhases) {
          attach(module, 0x11ae1fc, 'lookup_inner', {
            onEnter: function (args) {
              this.observerThread = Process.getCurrentThreadId();
              this.observerBatch = lookupScopes.get(this.observerThread) || null;
              if (this.observerBatch) {
                if (innerScopes.has(this.observerThread)) errors.push('nested inner scope');
                innerScopes.set(this.observerThread, this.observerBatch);
                try {
                  const object = args[0].and(ptr('0x00ffffffffffffff'));
                  const insertion = insertionHistory.get(object.add(0xf8).toString());
                  const population = populationHistory.get(object.toString());
                  const allocation = allocationHistory.get(object.toString());
                  const paths = pathCreationHistory.get(object.toString());
                  const sourcePaths = pathSourceHistory.get(object.toString());
                  const sourceParents = sourceParentHistory.get(object.toString());
                  const sourceDb = sourceDbHistory.get(object.toString());
                  this.tree = {root_nonnull: !object.add(0x100).readPointer().isNull(),
                               size_word: object.add(0x108).readU64().toString(),
                               prior_insertion: insertion ? JSON.parse(JSON.stringify(insertion))
                                 : {calls: 0, returns: 0, callers: {}},
                               prior_population: population ? JSON.parse(JSON.stringify(population))
                                 : {calls: 0, returns: 0, callers: {}},
                               prior_allocation: allocation ? JSON.parse(JSON.stringify(allocation))
                                 : {calls: 0, returns: 0, callers: {}},
                               prior_paths: paths ? Array.from(paths.values(), item => ({...item})) : [],
                               prior_source_paths: sourcePaths ? Array.from(sourcePaths.values(), item => ({...item})) : [],
                               source_parents: sourceParents ? {...sourceParents} : {},
                               source_db_init: sourceDb ? {...sourceDb} : {}};
                } catch (error) { errors.push('lookup tree: ' + String(error)); }
              }
            },
            onLeave: function (value) {
              if (this.observerBatch) {
                record({kind: 'lookup_inner', batch: this.observerBatch, nonnull: !value.isNull(), tree: this.tree});
                if (innerScopes.get(this.observerThread) !== this.observerBatch) errors.push('inner scope mismatch');
                innerScopes.delete(this.observerThread);
              }
            }
          });
          attach(module, 0x11b20f0, 'first_lookup_result', {
            onEnter: function (args) {
              this.observerBatch = innerScopes.get(Process.getCurrentThreadId()) || null;
              if (this.observerBatch) this.sentinel = args[0].and(ptr('0x00ffffffffffffff')).add(8);
            },
            onLeave: function (value) {
              if (!this.observerBatch) return;
              const result = value.and(ptr('0x00ffffffffffffff'));
              if (result.isNull()) { errors.push('null first lookup result'); return; }
              record({kind: 'first_lookup_result', batch: this.observerBatch,
                      found: !result.equals(this.sentinel)});
            }
          });
          attach(module, 0x1207ffc, 'finish_lookup', {
            onEnter: function (args) {
              this.observerBatch = finishScopes.get(Process.getCurrentThreadId()) || null;
              if (this.observerBatch) {
                this.observerThread = Process.getCurrentThreadId();
                if (lookupScopes.has(this.observerThread)) errors.push('nested lookup scope');
                lookupScopes.set(this.observerThread, this.observerBatch);
                this.observerKey = nativeString16(args[1]);
                try { this.observerInput = lookupInput(args[0]); }
                catch (error) { errors.push('lookup input: ' + String(error)); }
              }
            },
            onLeave: function (value) {
              if (this.observerBatch) {
                record({kind: 'finish_lookup', batch: this.observerBatch,
                        nonnull: !value.isNull(), key: this.observerKey, lookupInput: this.observerInput});
                if (lookupScopes.get(this.observerThread) !== this.observerBatch) errors.push('lookup scope mismatch');
                lookupScopes.delete(this.observerThread);
              }
            }
          });
          for (const [label, offset] of [['finish_a', 0xe1c290], ['finish_b', 0xdcd238]]) {
            attach(module, offset, label, {
              onEnter: function (args) {
                this.observerBatch = batchScopes.get(Process.getCurrentThreadId()) || null;
                if (!this.observerBatch) return;
                this.observerContext = args[0];
                this.observerThread = Process.getCurrentThreadId();
                if (label === 'finish_a') {
                  if (finishScopes.has(this.observerThread)) errors.push('nested finish scope');
                  finishScopes.set(this.observerThread, this.observerBatch);
                }
                const event = {kind: 'batch_phase', phase: label, edge: 'before', batch: this.observerBatch};
                try {
                  event.tableCounts = tableCounts(this.observerContext);
                  if (label === 'finish_a') {
                    const root = this.observerContext.and(ptr('0x00ffffffffffffff'));
                    const slot = root.add(0xb60);
                    const range = Process.findRangeByAddress(slot);
                    if (!range || !range.protection.includes('r') || slot.add(8).compare(range.base.add(range.size)) > 0)
                      throw new Error('context slot unreadable');
                    event.contextSlotB60Nonnull = !slot.readPointer().isNull();
                    if (engineDbOwner) {
                      const content = module.base.add(0x19ad878).readPointer().and(ptr('0x00ffffffffffffff'));
                      const currentContext = content.isNull() ? ptr(0) : content.add(0x20).readPointer().and(ptr('0x00ffffffffffffff'));
                      event.owner_matches_later_root = engineDbOwner.equals(content);
                      event.owner_matches_later_context = engineDbOwner.equals(currentContext);
                      event.later_root_nonnull = !content.isNull();
                      event.later_context_nonnull = !currentContext.isNull();
                    }
                  }
                }
                catch (error) { errors.push(label + ' before: ' + String(error)); }
                record(event);
              },
              onLeave: function () {
                if (!this.observerBatch) return;
                const event = {kind: 'batch_phase', phase: label, edge: 'after', batch: this.observerBatch};
                try { event.tableCounts = tableCounts(this.observerContext); }
                catch (error) { errors.push(label + ' after: ' + String(error)); }
                if (label === 'finish_a') {
                  if (finishScopes.get(this.observerThread) !== this.observerBatch) errors.push('finish scope mismatch');
                  finishScopes.delete(this.observerThread);
                }
                record(event);
              }
            });
          }
        }
      }
      this.observerThreadId = Process.getCurrentThreadId();
      this.batchCall = ++nextCall;
      if (batchScopes.has(this.observerThreadId)) errors.push('nested batch');
      batchScopes.set(this.observerThreadId, this.batchCall);
      let listCount = null;
      try { listCount = args[1].and(ptr('0x00ffffffffffffff')).add(12).readS32(); } catch (_) {}
      const event = {kind: 'table_batch', call: this.batchCall, args: [2, 3, 4, 5, 6].map(i => args[i].toUInt32()), listCount: listCount,
        caller: this.returnAddress.sub(module.base).toString()};
      if (includeBatchCounts) {
        this.countsContext = args[0];
        try { event.tableCounts = tableCounts(this.countsContext); }
        catch (error) { errors.push('batch before: ' + String(error)); }
      }
      record(event);
    },
    onLeave: function () {
      if (includeBatchCounts) {
        const event = {kind: 'table_batch_end', call: this.batchCall};
        try { event.tableCounts = tableCounts(this.countsContext); }
        catch (error) { errors.push('batch after: ' + String(error)); }
        record(event);
      }
      if (batchScopes.get(this.observerThreadId) !== this.batchCall) errors.push('batch scope mismatch');
      batchScopes.delete(this.observerThreadId);
    }
  });
  attach(module, 0xdcd364, 'postprocess', {
    onEnter: function () { record({kind: 'postprocess', caller: this.returnAddress.sub(module.base).toString()}); }
  });
  attach(module, 0x12e03e4, 'filesystem_open', {
    onEnter: function (args) {
      this.path = engineString(args[1]);
      this.observerThreadId = Process.getCurrentThreadId();
      this.callId = ++nextCall;
      this.databaseParent = dbCtorScopes.get(this.observerThreadId) || null;
      this.parent = scopes.get(this.observerThreadId) || null;
      scopes.set(this.observerThreadId, this.callId);
    },
    onLeave: function (value) {
      let vtable = null;
      if (!value.isNull()) {
        try {
          const table = value.and(ptr('0x00ffffffffffffff')).readPointer().and(ptr('0x00ffffffffffffff'));
          const owner = Process.findModuleByAddress(table);
          vtable = owner && owner.name === 'libg.so' ? table.sub(owner.base).toString() : 'external';
        } catch (_) { vtable = 'unreadable'; }
      }
      record({kind: 'filesystem_open', path: this.path, nonnull: !value.isNull(), vtable: vtable,
        call: this.callId, parent: this.parent, database_parent: this.databaseParent});
      if (scopes.get(this.observerThreadId) !== this.callId) errors.push('filesystem scope mismatch');
      if (this.parent) scopes.set(this.observerThreadId, this.parent); else scopes.delete(this.observerThreadId);
    }
  });
  record({kind: 'engine_hooks_ready'});
}
rpc.exports = {
  install: function (batchCounts, batchPhases) {
    includeBatchCounts = batchCounts === true;
    includeBatchPhases = batchPhases === true;
    Process.attachModuleObserver({onAdded: observe});
    if (includeBatchPhases) {
      const libc = Process.findModuleByName('libc.so');
      for (const name of ['open', 'openat']) {
        const entry = libc && libc.findExportByName(name);
        if (!entry) { coverage.push({label: 'libc_' + name, installed: false}); continue; }
        Interceptor.attach(entry, {
          onEnter: function (args) {
            this.dbParent = dbCtorScopes.get(Process.getCurrentThreadId()) || null;
            if (this.dbParent) this.dbPath = boundedString(args[name === 'open' ? 0 : 1]);
          },
          onLeave: function (value) {
            if (this.dbParent) record({kind: 'database_libc_open', api: name, parent: this.dbParent,
                                       path: this.dbPath, fd: value.toInt32()});
          }
        });
        coverage.push({label: 'libc_' + name, installed: true});
      }
    }
    const android = Process.findModuleByName('libandroid.so');
    if (android) {
      const open = android.findExportByName('AAssetManager_open');
      if (open) {
        Interceptor.attach(open, {
          onEnter: function (args) { this.path = boundedString(args[1]); this.parent = scopes.get(Process.getCurrentThreadId()) || null; },
          onLeave: function (value) { record({kind: 'asset_open', path: this.path, parent: this.parent, nonnull: !value.isNull()}); }
        });
        coverage.push({label: 'asset_open', installed: true});
      }
    }
    return {coverage: coverage, errors: errors};
  },
  snapshot: function () { return {events: events, coverage: coverage, errors: errors, lost: lost,
    activeScopes: scopes.size, activeBatchScopes: batchScopes.size, activeFinishScopes: finishScopes.size,
    activeLookupScopes: lookupScopes.size, activeInnerScopes: innerScopes.size,
    activeSourceLoaderScopes: sourceLoaderScopes.size, activeDbCtorScopes: dbCtorScopes.size,
    activeDbInitScopes: dbInitScopes.size, activeDbFactoryScopes: dbFactoryScopes.size,
    activeEngineDbScopes: engineDbScopes.size}; }
};
