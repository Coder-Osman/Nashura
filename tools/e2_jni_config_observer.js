'use strict';

let configuration = null;
let hostPhase = 'process_start';
let sequence = 0;
const listeners = [];
const installed = new Set();
const envTables = new Set();
const vmTables = new Set();
const phaseStacks = new Map();
const classNames = new Map();
const classAliases = new Map();
const methodIds = new Map();
const stringLengths = new Map();
const utf8Lengths = new Map();
const arrayLengths = new Map();
const functionCounts = new Map();
const configCalls = [];
const jniCounts = new Map();
const jniMethods = new Map();
const capturedStrings = new Map();
const referenceCounts = new Map();
const registeredNatives = [];
const vmCounts = new Map();
const errors = [];
const coverage = [];
const dropped = {errors: 0, config_calls: 0, jni_samples: 0};

const JNI_DETAIL = new Set([
  'FindClass', 'GetMethodID', 'GetStaticMethodID', 'GetFieldID', 'GetStaticFieldID',
  'RegisterNatives', 'UnregisterNatives', 'GetJavaVM', 'ExceptionOccurred',
  'ExceptionCheck', 'ExceptionClear', 'ExceptionDescribe', 'Throw', 'ThrowNew',
  'NewString', 'NewStringUTF', 'GetStringLength', 'GetStringUTFLength',
  'GetStringChars', 'ReleaseStringChars', 'GetStringUTFChars', 'ReleaseStringUTFChars',
  'GetStringRegion', 'GetStringUTFRegion', 'GetArrayLength', 'GetByteArrayElements',
  'ReleaseByteArrayElements', 'GetByteArrayRegion', 'SetByteArrayRegion',
  'GetPrimitiveArrayCritical', 'ReleasePrimitiveArrayCritical', 'NewGlobalRef',
  'DeleteGlobalRef', 'NewLocalRef', 'DeleteLocalRef', 'NewWeakGlobalRef',
  'DeleteWeakGlobalRef', 'IsSameObject', 'GetObjectRefType', 'GetObjectClass', 'MonitorEnter', 'MonitorExit'
]);

function location(address) {
  const module = Process.findModuleByAddress(address);
  return module ? module.name + '+' + address.sub(module.base) : 'unmapped';
}

function phase(thread) {
  const stack = phaseStacks.get(thread) || [];
  return stack.length ? stack[stack.length - 1].name : hostPhase;
}

function phaseChain(thread) {
  return (phaseStacks.get(thread) || []).map(entry => entry.name);
}

function error(kind, detail) {
  if (errors.length < 256) errors.push({sequence: sequence++, phase: hostPhase, kind, detail: String(detail)});
  else dropped.errors++;
}

function readUtf8(pointer, limit) {
  if (pointer.isNull()) return {status: 'null'};
  try {
    const range = Process.findRangeByAddress(pointer);
    if (!range || !range.protection.includes('r')) return {status: 'unreadable'};
    const bytes = [];
    for (let index = 0; index < limit; index++) {
      if (pointer.add(index).compare(range.base.add(range.size)) >= 0) return {status: 'range_overrun'};
      const byte = pointer.add(index).readU8();
      if (byte === 0) return decodeModifiedUtf8(bytes);
      bytes.push(byte);
    }
    return {status: 'unterminated_or_truncated', bytes: bytes.length};
  } catch (failure) {
    return {status: 'decode_error', error: String(failure)};
  }
}

function decodeModifiedUtf8(bytes) {
  let value = '';
  for (let index = 0; index < bytes.length;) {
    const first = bytes[index++];
    if (first < 128) value += String.fromCharCode(first);
    else {
      const width = (first & 224) === 192 ? 2 : (first & 240) === 224 ? 3 : 0;
      if (!width || index + width - 1 > bytes.length) return {status: 'invalid_mutf8', raw_hex: bytes.map(byte => byte.toString(16).padStart(2, '0')).join('')};
      let unit = first & (width === 2 ? 31 : 15);
      for (let part = 1; part < width; part++) {
        const next = bytes[index++];
        if ((next & 192) !== 128) return {status: 'invalid_mutf8'};
        unit = (unit << 6) | (next & 63);
      }
      if ((width === 2 && unit < 128 && unit !== 0) || (width === 3 && unit < 2048)) return {status: 'invalid_mutf8'};
      value += String.fromCharCode(unit);
    }
  }
  return {status: 'decoded', encoding: 'JNI modified UTF-8', byte_length: bytes.length, value};
}

function readCppString(pointer) {
  if (pointer.isNull()) return {status: 'null'};
  try {
    const tag = pointer.readU8();
    const length = tag & 1 ? Number(pointer.add(8).readU64().toString()) : tag >>> 1;
    if (!Number.isSafeInteger(length) || length > 4096 || (!(tag & 1) && length > 22)) return {status: 'length_out_of_range', length};
    const data = tag & 1 ? pointer.add(16).readPointer() : pointer.add(1);
    if (data.isNull() || data.add(length).readU8() !== 0) return {status: 'missing_terminator', length};
    return {status: 'decoded', abi: 'libc++24-lsb-long', length, value: length ? data.readUtf8String(length) : ''};
  } catch (failure) { return {status: 'decode_error', error: String(failure)}; }
}

function readEngineString(pointer) {
  if (pointer.isNull()) return {status: 'null'};
  try {
    const length = pointer.add(4).readU32();
    if (length > 4096) return {status: 'length_out_of_range', length};
    const data = length < 8 ? pointer.add(8) : pointer.add(8).readPointer().and('0x00ffffffffffffff');
    if (data.isNull()) return {status: 'null_data', length};
    const range = Process.findRangeByAddress(data);
    if (!range || !range.protection.includes('r')) return {status: 'unreadable_data', length};
    const end = data.add(length + 1);
    if (end.compare(range.base.add(range.size)) > 0) return {status: 'range_overrun', length};
    const value = length ? data.readUtf8String(length) : '';
    if (data.add(length).readU8() !== 0) return {status: 'missing_terminator', length, value};
    return {status: 'decoded', length, value};
  } catch (failure) {
    return {status: 'decode_error', error: String(failure)};
  }
}

function readUtf16(pointer, length) {
  if (pointer.isNull()) return {status: 'null'};
  if (!Number.isInteger(length) || length < 0 || length > 4096) return {status: 'length_unknown_or_out_of_range', length};
  try {
    const range = Process.findRangeByAddress(pointer);
    if (!range || !range.protection.includes('r') ||
        pointer.add(length * 2).compare(range.base.add(range.size)) > 0) return {status: 'unreadable', length};
    let value = '';
    for (let offset = 0; offset < length; offset += 256) {
      const finish = Math.min(length, offset + 256);
      const units = [];
      for (let index = offset; index < finish; index++) units.push(pointer.add(index * 2).readU16());
      value += String.fromCharCode.apply(null, units);
    }
    return {status: 'decoded', length, value};
  } catch (failure) {
    return {status: 'decode_error', length, error: String(failure)};
  }
}

function addCount(map, key, value) {
  let row = map.get(key);
  if (!row) {
    row = Object.assign({count: 0, first_sequence: sequence, last_sequence: sequence}, value);
    map.set(key, row);
  }
  row.count++;
  row.last_sequence = sequence++;
  return row;
}

function captureText(source, text, metadata) {
  if (!text || text.status !== 'decoded') return;
  const value = text.value;
  const key = source + '\u0000' + value;
  if (!capturedStrings.has(key)) capturedStrings.set(key, Object.assign({source, value}, metadata));
}

function methodDescriptor(env, name) {
  const entry = methodIds.get(name.toString());
  return entry || {method_name_status: 'unknown'};
}

function stringResultSample(name, pointer, length, metadata) {
  const decoded = readUtf8(pointer, 4096);
  captureText(name, decoded, metadata);
  return decoded.status === 'decoded' ? {status: 'decoded', length: decoded.value.length, value: decoded.value} : decoded;
}

function byteHash(pointer, length) {
  if (pointer.isNull()) return {status: 'null', length};
  if (!Number.isInteger(length) || length < 0 || length > 1024 * 1024) return {status: 'length_out_of_range', length};
  try {
    let hash = 2166136261;
    for (let index = 0; index < length; index++) hash = Math.imul(hash ^ pointer.add(index).readU8(), 16777619) >>> 0;
    return {status: 'hashed_fnv1a32', length, fnv1a32: hash.toString(16).padStart(8, '0')};
  } catch (failure) {
    return {status: 'hash_error', length, error: String(failure)};
  }
}

function jniEvent(name, caller, thread, extra) {
  const chain = phaseChain(thread);
  const key = [name, caller, thread, phase(thread), chain.join('>')].join('|');
  const count = addCount(jniCounts, key, {name, caller, thread, phase: phase(thread), phase_chain: chain, samples: []});
  const info = Object.assign({sequence: sequence++, name, caller, thread,
    phase: phase(thread), phase_chain: chain}, extra || {});
  if (info.method_id) {
    const method = methodDescriptor(null, info.method_id);
    if (method.method_name_status !== 'unknown') info.method = method;
  }
  const methodKey = info.method ? [name, info.method.class_name || '', info.method.name || '', info.method.signature || '', caller, thread, phase(thread)].join('|') : null;
  if (methodKey) addCount(jniMethods, methodKey, {name, caller, thread, phase: phase(thread), method: info.method});
  if (count.samples.length < (configuration.sample_limit || 3)) count.samples.push(info);
  else dropped.jni_samples++;
  return info;
}

function safePointer(value) {
  try { return value.toString(); } catch (_) { return 'unavailable'; }
}

function watchFunction(target, label, options) {
  const addressKey = target.toString() + ':' + label;
  if (installed.has(addressKey)) return;
  installed.add(addressKey);
  const entry = {name: label, target: location(target), options: options || {}};
  try {
    const range = Process.findRangeByAddress(target);
    if (!range || !range.protection.includes('x')) throw new Error('target not executable');
    listeners.push(Interceptor.attach(target, {
      onEnter(args) {
        const thread = Process.getCurrentThreadId();
        let active = phaseStacks.get(thread) || [];
        if (entry.options.step) {
          let tick = null;
          try { tick = args[0].add(configuration.tick_offset).readS32(); } catch (failure) { error('tick_read', failure); }
          const name = tick === 0 ? 'first_tick' : tick < 1200 ? 'sustained_early' :
            tick < 3600 ? 'sustained_middle' : 'sustained_late_terminal';
          active = active.concat([{name, tick}]);
        } else if (entry.options.phase) active = active.concat([{name: entry.options.phase}]);
        phaseStacks.set(thread, active);
        this.thread = thread;
        this.caller = location(this.returnAddress);
        this.phaseName = phase(thread);
        this.phaseChain = phaseChain(thread);
        this.entryName = label;
        this.output = null;
        if ((configuration.stream_native || []).includes(label)) send({event: 'e2_native_entry',
          name: label, thread, phase: this.phaseName, caller: this.caller, arguments: [safePointer(args[0]), safePointer(args[1])]});
        if (label === 'JNI_OnLoad') watchVmTable(args[0]);
        if (label === 'PreferenceRead' || label === 'PreferenceExists' ||
            label === 'PreferenceWrite' || label.endsWith('ReadBridge') || label.endsWith('WriteBridge')) {
          const row = {sequence: sequence++, operation: label, thread, phase: this.phaseName,
            phase_chain: this.phaseChain, caller: this.caller, key: label === 'PreferenceJNIReadBridge' ? readCppString(args[1]) : readEngineString(args[0]),
            arguments: [safePointer(args[0]), safePointer(args[1])], result: null};
          if (label === 'PreferenceWrite' || label === 'PreferenceNativeWriteBridge') row.value = readEngineString(args[1]);
          if (label === 'PreferenceRead' || label.includes('ReadBridge')) {
            this.output = this.context.x8;
            row.output_abi = 'AArch64 indirect aggregate result in x8; observed pointer retained';
          }
          this.configRow = row;
          if (configCalls.length < 16384) configCalls.push(row);
          else dropped.config_calls++;
        }
        const key = [label, this.caller, thread, this.phaseName, this.phaseChain.join('>')].join('|');
        addCount(functionCounts, key, {name: label, caller: this.caller, thread,
          phase: this.phaseName, phase_chain: this.phaseChain});
      },
      onLeave(retval) {
        if (this.configRow) {
          const row = this.configRow;
          if (this.output && !this.output.isNull()) row.returned_string = label === 'PreferenceJNIReadBridge' ? readCppString(this.output) : readEngineString(this.output);
          else row.return_value = retval.toString();
          if (label === 'PreferenceExists') row.boolean_result = retval.toInt32() !== 0;
        }
        const thread = this.thread;
        const stack = phaseStacks.get(thread) || [];
        if (entry.options.step || entry.options.phase) stack.pop();
        if (stack.length) phaseStacks.set(thread, stack); else phaseStacks.delete(thread);
        if (entry.options.bootstrap) installTrampolines();
      }
    }));
    coverage.push({name: label, target: entry.target, status: 'attached'});
  } catch (failure) {
    error('native_hook', label + ': ' + failure);
    coverage.push({name: label, target: entry.target, status: 'failed', error: String(failure)});
  }
}

function propagateReference(source, destination) {
  const from = source.toString();
  const to = destination.toString();
  if (classNames.has(from)) classNames.set(to, classNames.get(from));
  if (classAliases.has(from)) classAliases.set(to, classAliases.get(from));
}

function watchJniTable(environment) {
  if (environment.isNull()) return;
  try {
    const table = environment.readPointer();
    const identity = table.toString();
    if (envTables.has(identity)) return;
    envTables.add(identity);
    const grouped = new Map();
    for (let index = 0; index < configuration.jni_names.length; index++) {
      const name = configuration.jni_names[index];
      if (!name) continue;
      let target;
      try { target = table.add(index * Process.pointerSize).readPointer(); }
      catch (failure) { error('jni_table_read', name + ': ' + failure); continue; }
      if (target.isNull()) { coverage.push({name, index, table: 'JNIEnv', status: 'null_slot'}); continue; }
      const key = target.toString();
      let entry = grouped.get(key);
      if (!entry) { entry = {target, names: [], indices: []}; grouped.set(key, entry); }
      entry.names.push(name);
      entry.indices.push(index);
    }
    for (const entry of grouped.values()) {
      const targetKey = 'jni:' + entry.target.toString();
      if (installed.has(targetKey)) continue;
      installed.add(targetKey);
      try {
      listeners.push(Interceptor.attach(entry.target, {
        onEnter(args) {
          this.thread = Process.getCurrentThreadId();
          this.caller = location(this.returnAddress);
          this.jniArgs = [args[0], args[1], args[2], args[3], args[4]];
          this.events = [];
          for (const name of entry.names.length === 1 ? entry.names : ['ambiguous_alias']) {
            let extra = {};
            extra.aliases = entry.names;
            extra.indices = entry.indices;
            if (name === 'FindClass') extra.class_request = readUtf8(args[1], 1024);
            if (['GetMethodID','GetStaticMethodID','GetFieldID','GetStaticFieldID'].includes(name)) {
              extra.class_ref = safePointer(args[1]);
              extra.member_name = readUtf8(args[2], 1024);
              extra.signature = readUtf8(args[3], 2048);
            }
            if (name === 'NewStringUTF') {
              extra.input_string = readUtf8(args[1], 4096);
              captureText(name, extra.input_string, {caller: this.caller, thread: this.thread, phase: phase(this.thread)});
            }
            if (name === 'NewString') {
              extra.input_string = readUtf16(args[1], args[2].toInt32());
              captureText(name, extra.input_string, {caller: this.caller, thread: this.thread, phase: phase(this.thread)});
            }
            if (name.startsWith('Call') || name.startsWith('NewObject')) {
              const methodId = name.startsWith('CallNonvirtual') ? args[3] : args[2];
              extra.method_id = safePointer(methodId);
            }
            if (name === 'GetStringUTFChars' || name === 'GetStringChars' || name === 'GetStringUTFLength' ||
                name === 'GetStringLength' || name.includes('ArrayElements') || name === 'GetArrayLength' ||
                name === 'GetByteArrayRegion' || name === 'SetByteArrayRegion' || name === 'GetStringRegion' ||
                name === 'GetStringUTFRegion' || name.includes('GlobalRef') || name.includes('LocalRef') ||
                name === 'GetObjectRefType' || name === 'IsSameObject' || name === 'GetObjectClass') extra.object_ref = safePointer(args[1]);
            if (name === 'GetByteArrayRegion' || name === 'SetByteArrayRegion') {
              extra.start = args[2].toInt32(); extra.length = args[3].toInt32();
              if (name === 'SetByteArrayRegion') extra.bytes = byteHash(args[4], extra.length);
            }
            if (name === 'GetStringRegion' || name === 'GetStringUTFRegion') {
              extra.start = args[2].toInt32(); extra.length = args[3].toInt32();
            }
            if (name === 'RegisterNatives') {
              const count = args[3].toInt32();
              extra.registered = [];
              if (count >= 0 && count <= 256) {
                for (let item = 0; item < count; item++) {
                  const row = args[2].add(item * Process.pointerSize * 3);
                  const methodName = readUtf8(row.readPointer(), 1024);
                  const signature = readUtf8(row.add(Process.pointerSize).readPointer(), 2048);
                  const target = row.add(Process.pointerSize * 2).readPointer();
                  const native = {class_ref: safePointer(args[1]), name: methodName,
                    signature, target: location(target)};
                  extra.registered.push(native);
                  registeredNatives.push(Object.assign({sequence: sequence++, thread: this.thread,
                    phase: phase(this.thread), caller: this.caller}, native));
                }
              } else extra.registration_status = 'count_out_of_range';
            }
            if (name === 'GetJavaVM') this.vmOut = args[1];
            extra.table_index = entry.indices[entry.names.indexOf(name)];
            const event = jniEvent(name, this.caller, this.thread, extra);
            this.events.push({name, extra, event});
          }
        },
        onLeave(retval) {
          for (const item of this.events || []) {
            const {name, extra, event} = item;
            const jniArgs = this.jniArgs;
            let result = null;
            try {
              if (name === 'FindClass' && !retval.isNull()) {
                const text = extra.class_request;
                if (text && text.status === 'decoded') classNames.set(retval.toString(), text.value);
              }
              if (['GetMethodID','GetStaticMethodID','GetFieldID','GetStaticFieldID'].includes(name) && !retval.isNull()) {
                const className = null;
                const entry = {name: extra.member_name && extra.member_name.value,
                  signature: extra.signature && extra.signature.value, class_name: className,
                  kind: name.includes('Field') ? 'field' : 'method', static: name.startsWith('GetStatic'),
                  class_identity_status: 'unknown; raw local handles may be reused'};
                methodIds.set(retval.toString(), entry);
                if (entry.name && entry.signature) {
                  const key = [entry.class_name || '?', entry.name, entry.signature, entry.kind, entry.static].join('|');
                  if (!jniMethods.has(key)) jniMethods.set(key, {name: entry.name, signature: entry.signature,
                    class_name: entry.class_name, kind: entry.kind, static: entry.static,
                    first_sequence: sequence++, count: 0});
                  jniMethods.get(key).count++;
                }
              }
              if (name === 'NewGlobalRef' || name === 'NewLocalRef' || name === 'NewWeakGlobalRef') {
                if (!retval.isNull()) propagateReference(jniArgs[1], retval);
                const refName = name;
                const key = [refName, this.caller, this.thread, phase(this.thread)].join('|');
                addCount(referenceCounts, key, {operation: refName, caller: this.caller,
                  thread: this.thread, phase: phase(this.thread)});
                result = {non_null: !retval.isNull()};
              } else if (name.startsWith('Delete') && name.endsWith('Ref')) {
                classNames.delete(jniArgs[1].toString());
                classAliases.delete(jniArgs[1].toString());
                stringLengths.delete(jniArgs[1].toString());
                utf8Lengths.delete(jniArgs[1].toString());
                arrayLengths.delete(jniArgs[1].toString());
                const key = [name, this.caller, this.thread, phase(this.thread)].join('|');
                addCount(referenceCounts, key, {operation: name, caller: this.caller,
                  thread: this.thread, phase: phase(this.thread)});
              } else if (name === 'GetStringLength' || name === 'GetStringUTFLength') {
                const length = retval.toInt32();
                (name === 'GetStringLength' ? stringLengths : utf8Lengths).set(jniArgs[1].toString(), length); result = {length};
              } else if (name === 'GetArrayLength') {
                const length = retval.toInt32(); arrayLengths.set(jniArgs[1].toString(), length); result = {length};
              } else if (name === 'GetStringUTFChars') {
                result = stringResultSample(name, retval, utf8Lengths.get(jniArgs[1].toString()),
                  {caller: this.caller, thread: this.thread, phase: phase(this.thread)});
              } else if (name === 'GetStringChars') {
                result = readUtf16(retval, stringLengths.get(jniArgs[1].toString()));
                captureText(name, result, {caller: this.caller, thread: this.thread, phase: phase(this.thread)});
              } else if (name === 'GetByteArrayElements') {
                result = byteHash(retval, arrayLengths.get(jniArgs[1].toString()));
              } else if (name === 'GetPrimitiveArrayCritical') result = {status: 'unknown_element_width', non_null: !retval.isNull()};
              else if (name === 'GetByteArrayRegion') result = byteHash(jniArgs[4], extra.length);
              else if (name === 'ExceptionCheck') result = {pending: retval.toInt32() !== 0};
              else if (name === 'ExceptionOccurred') result = {pending: !retval.isNull()};
              else if (name === 'GetObjectRefType') result = {ref_type: retval.toInt32()};
              else if (name === 'IsSameObject') result = {same: retval.toInt32() !== 0};
              else if (name === 'GetObjectClass') result = {class_ref: safePointer(retval), class_name: null, class_identity_status: 'unknown; raw handles may be reused'};
              else if (name.startsWith('Call') || name.startsWith('NewObject')) {
                const method = methodDescriptor(null, extra.method_id);
                if (method.method_name_status === 'unknown') result = {method, raw_return: retval.toString()};
                else {
                  const returnType = (method.signature || '').split(')').pop();
                  if (returnType.startsWith('L') || returnType.startsWith('[')) result = {method, non_null: !retval.isNull()};
                  else if (returnType === 'V') result = {method, returned: 'void'};
                  else if (returnType === 'F' || returnType === 'D') result = {method, status: 'unknown_vector_register_return'};
                  else result = {method, raw_return: retval.toString()};
                }
              } else if (name === 'GetJavaVM' || name === 'RegisterNatives' || name === 'UnregisterNatives' || name === 'Throw' || name === 'ThrowNew') {
                result = {code: retval.toInt32()};
                if (name === 'GetJavaVM' && retval.toInt32() === 0 && this.vmOut) {
                  const vm = this.vmOut.readPointer();
                  if (!vm.isNull()) watchVmTable(vm);
                }
              }
            } catch (failure) { error('jni_result', name + ': ' + failure); }
            event.result = result || {status: 'uninterpreted', raw_return: retval.toString()};
          }
        }
      }));
      coverage.push({name: entry.names, indices: entry.indices, table: 'JNIEnv', target: location(entry.target), status: 'attached'});
      } catch (failure) {
        error('jni_hook', failure);
        coverage.push({name: entry.names, indices: entry.indices, table: 'JNIEnv', status: 'failed', error: String(failure)});
      }
    }
  } catch (failure) { error('jni_table', failure); }
}

function watchVmTable(vm) {
  if (vm.isNull()) return;
  try {
    const table = vm.readPointer();
    const identity = table.toString();
    if (vmTables.has(identity)) return;
    vmTables.add(identity);
    const names = configuration.vm_names || [null, null, null, 'DestroyJavaVM',
      'AttachCurrentThread', 'DetachCurrentThread', 'GetEnv', 'AttachCurrentThreadAsDaemon'];
    for (let index = 0; index < names.length; index++) {
      const name = names[index];
      if (!name) continue;
      const target = table.add(index * Process.pointerSize).readPointer();
      listeners.push(Interceptor.attach(target, {
        onEnter(args) {
          this.thread = Process.getCurrentThreadId(); this.caller = location(this.returnAddress);
          this.name = name; this.out = null;
          if (name === 'GetEnv' || name.startsWith('AttachCurrentThread')) this.out = args[1];
          const key = [name, this.caller, this.thread, phase(this.thread)].join('|');
          addCount(vmCounts, key, {name, caller: this.caller, thread: this.thread, phase: phase(this.thread)});
        },
        onLeave(result) {
          let env = null;
          if (this.out && result.toInt32() === 0) {
            try { env = this.out.readPointer(); if (!env.isNull()) watchJniTable(env); }
            catch (failure) { error('vm_env_output', failure); }
          }
          const key = [name, this.caller, this.thread, phase(this.thread)].join('|');
          const row = vmCounts.get(key);
          if (row && row.samples === undefined) row.samples = [];
          if (row && row.samples.length < 3) row.samples.push({result: result.toInt32(), env_obtained: Boolean(env)});
        }
      }));
      coverage.push({name, table: 'JavaVM', target: location(target), status: 'attached'});
    }
  } catch (failure) { error('vm_table', failure); }
}

function moduleAdded(module) {
  if (!configuration) return;
  if (module.name === configuration.probe_module) {
    const target = module.enumerateExports().find(entry => entry.type === 'function' && entry.name === configuration.bootstrap_export);
    if (target) watchFunction(target.address, 'probe.bootstrap', {phase: 'probe_bootstrap', bootstrap: true});
  }
  if (module.name !== configuration.engine_module) return;
  const byName = new Map(configuration.functions.map(entry => [entry.name, entry]));
  for (const name of ['JNI_OnLoad', 'GamePreinit', 'GamePreinitConfigure', 'ContentBootstrap',
    'SCDBOpen', 'TablePreload', 'TableLoadBatch', 'StateMaskStage', 'IDRegistration',
    'ContentPostprocess', 'GameStateConstructor', 'ConfigurationConstructor', 'GameConfigurationInit',
    'PreferenceRead', 'PreferenceExists', 'PreferenceWrite', 'PreferenceNativeReadBridge',
    'PreferenceJNIReadBridge', 'PreferenceNativeWriteBridge', 'GameStateSetup',
    'GameStateDestroy', 'GameStateBaseDestroy', 'LogicCommandParse', 'LogicCommandAppend',
    'StateSnapshot', 'GameStateSerializeHeader', 'GameStateSerializeBody',
    'GameStateDeserializeHeader', 'GameStateDeserializeBody', 'JNIEnvAttach', 'JNIClassResolve', 'JNIExceptionCheck']) {
    const entry = byName.get(name);
    if (!entry) continue;
    const options = Object.assign({}, entry.options || {});
    const phaseByName = {
      JNI_OnLoad: 'load', GamePreinit: 'preinit', GamePreinitConfigure: 'preinit',
      ContentBootstrap: 'resources', SCDBOpen: 'resources', TablePreload: 'resources',
      TableLoadBatch: 'resources', StateMaskStage: 'resources', IDRegistration: 'resources',
      ContentPostprocess: 'resources', GameStateConstructor: 'state_create',
      ConfigurationConstructor: 'configuration', GameConfigurationInit: 'configuration'
    };
    if (phaseByName[name]) options.phase = phaseByName[name];
    watchFunction(module.base.add(entry.offset), name, options);
  }
  coverage.push({name: 'libg.so', status: 'module_observed', base: location(module.base)});
}

function installTrampolines() {
  const probe = Process.getModuleByName(configuration.probe_module);
  for (const entry of configuration.trampolines || []) {
    try { watchFunction(probe.base.add(entry.pointer_offset).readPointer(), entry.name, entry.options || {}); }
    catch (failure) { error('trampoline_hook', entry.name + ': ' + failure); }
  }
}

rpc.exports = {
  configure(value) {
    if (configuration) throw new Error('configuration is immutable');
    if (!value || value.engine_module !== 'libg.so' || !Array.isArray(value.functions) ||
        !Array.isArray(value.jni_names)) throw new Error('incomplete E2 observer configuration');
    configuration = value;
    Process.attachModuleObserver({onAdded: moduleAdded});
    return true;
  },
  phase(value) { hostPhase = String(value); return true; },
  snapshot() {
    return {schema: 'e2-jni-config-observation.v1', engine_loaded: coverage.some(row => row.name === 'libg.so'),
      observation_scope: 'configured JavaVM/JNIEnv entries, config functions, and selected engine phase hooks; non-exhaustive',
      config_functions: Array.from(functionCounts.values()), config_calls: configCalls,
      jni_counts: Array.from(jniCounts.values()), jni_methods: Array.from(jniMethods.values()),
      captured_strings: Array.from(capturedStrings.values()), reference_counts: Array.from(referenceCounts.values()),
      registered_natives: registeredNatives, java_vm_counts: Array.from(vmCounts.values()),
      coverage, errors, dropped, reference_lifecycle_status: 'operation counts only; handle reuse and implicit local-frame retirement unknown',
      unknowns: ['unconfigured JNI function targets', 'unresolved Java object class identities',
        'E0 short stubs manager_b0.slot1 and command.slot2', 'OS process teardown outside observed probe']};
  }
};
