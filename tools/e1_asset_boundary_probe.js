'use strict';

rpc.exports = {
  compare(config) {
    const art = Process.getModuleByName('libart.so');
    const getVMs = new NativeFunction(art.getExportByName('JNI_GetCreatedJavaVMs'),
      'int', ['pointer', 'int', 'pointer']);
    const machines = Memory.alloc(Process.pointerSize);
    const count = Memory.alloc(4);
    if (getVMs(machines, 1, count) !== 0 || count.readS32() !== 1)
      throw new Error('expected one existing Java VM');
    const machine = machines.readPointer();
    function machineMethod(slot, result, arguments_) {
      return new NativeFunction(machine.readPointer().add(slot * Process.pointerSize).readPointer(),
        result, ['pointer', ...arguments_]);
    }
    const environmentOutput = Memory.alloc(Process.pointerSize);
    const attached = machineMethod(6, 'int', ['pointer', 'int'])(machine, environmentOutput, 0x10006);
    if (attached !== 0 && attached !== -2) throw new Error('GetEnv failed: ' + attached);
    if (attached === -2 && machineMethod(4, 'int', ['pointer', 'pointer'])(machine, environmentOutput, ptr(0)) !== 0)
      throw new Error('AttachCurrentThread failed');
    const environment = environmentOutput.readPointer();
    function method(slot, result, arguments_) {
      return new NativeFunction(environment.readPointer().add(slot * Process.pointerSize).readPointer(),
        result, ['pointer', ...arguments_]);
    }
    const exception = method(228, 'uchar', []);
    function checked(label, value) {
      if (exception(environment)) {
        method(17, 'void', [])(environment);
        throw new Error('JNI exception: ' + label);
      }
      return value;
    }
    const references = [];
    let manager = null;
    let close = null;
    try {
      const type = checked('FindClass', method(6, 'pointer', ['pointer'])(environment,
        Memory.allocUtf8String('android/content/res/AssetManager')));
      if (type.isNull()) throw new Error('AssetManager class missing');
      references.push(type);
      const getMethod = method(33, 'pointer', ['pointer', 'pointer', 'pointer']);
      function identifier(name, signature) {
        const result = checked(name, getMethod(environment, type,
          Memory.allocUtf8String(name), Memory.allocUtf8String(signature)));
        if (result.isNull()) throw new Error('missing method: ' + name);
        return result;
      }
      const constructor = identifier('<init>', '()V');
      manager = checked('new AssetManager', method(30, 'pointer', ['pointer', 'pointer', 'pointer'])(
        environment, type, constructor, ptr(0)));
      if (manager.isNull()) throw new Error('AssetManager allocation failed');
      references.push(manager);
      close = identifier('close', '()V');
      const path = checked('NewStringUTF', method(167, 'pointer', ['pointer'])(
        environment, Memory.allocUtf8String(config.archive)));
      references.push(path);
      const arguments_ = Memory.alloc(8);
      arguments_.writePointer(path);
      const cookie = checked('addAssetPath', method(51, 'int', ['pointer', 'pointer', 'pointer'])(
        environment, manager, identifier('addAssetPath', '(Ljava/lang/String;)I'), arguments_));
      if (cookie <= 0) throw new Error('fixture archive rejected by Android');
      const android = Process.getModuleByName('libandroid.so');
      const implementation = Module.load(config.provider);
      implementation.ensureInitialized();
      const initialize = new NativeFunction(implementation.getExportByName('e1_initialize'),
        'pointer', ['pointer', 'pointer', 'pointer']);
      const replacement = initialize(...[config.archive, config.index, config.audit]
        .map(value => Memory.allocUtf8String(value)));
      if (replacement.isNull()) throw new Error('fixture provider initialization failed');
      const original = new NativeFunction(android.getExportByName('AAssetManager_fromJava'),
        'pointer', ['pointer', 'pointer'])(environment, manager);
      if (original.isNull()) throw new Error('native manager missing');
      function inspect(module, prefix, context, name, mode) {
        function native(symbol, result, arguments_) {
          return new NativeFunction(module.getExportByName(prefix + symbol), result, arguments_);
        }
        const open = native('AAssetManager_open', 'pointer', ['pointer', 'pointer', 'int']);
        const asset = open(context, Memory.allocUtf8String(name), mode);
        const row = {opened: !asset.isNull()};
        if (asset.isNull()) return row;
        try {
          row.length = native('AAsset_getLength64', 'int64', ['pointer'])(asset).toString();
          if (Number(row.length) > 65536) throw new Error('fixture exceeds capacity');
          const output = Memory.alloc(65536);
          const read = native('AAsset_read', 'int', ['pointer', 'pointer', 'ulong']);
          const seek = native('AAsset_seek64', 'int64', ['pointer', 'int64', 'int']);
          const remaining = native('AAsset_getRemainingLength64', 'int64', ['pointer']);
          row.zero_read = read(asset, output, 0);
          row.read = read(asset, output, 65536);
          row.bytes = row.read >= 0 ? Array.from(new Uint8Array(output.readByteArray(row.read))) : null;
          row.remaining = remaining(asset).toString();
          row.again = read(asset, output, 1);
          row.seeks = [];
          for (const [offset, whence] of [[0, 0], [1, 2], [-1, 0], [0, 99], [0, 2], [0, 0]]) {
            const result = seek(asset, offset, whence).toString();
            const amount = read(asset, output, 1);
            row.seeks.push({offset, whence, result, read: amount,
              bytes: amount >= 0 ? Array.from(new Uint8Array(output.readByteArray(amount))) : null,
              remaining: remaining(asset).toString()});
          }
          const start = Memory.alloc(8);
          const length = Memory.alloc(8);
          start.writeS64(-1);
          length.writeS64(-1);
          const descriptor = native('AAsset_openFileDescriptor64', 'int', ['pointer', 'pointer', 'pointer'])(
            asset, start, length);
          row.descriptor = {opened: descriptor >= 0, start: start.readS64().toString(),
            length: length.readS64().toString()};
          if (descriptor >= 0) {
            const result = new NativeFunction(Process.getModuleByName('libc.so').getExportByName('close'),
              'int', ['int'])(descriptor);
            if (result !== 0) throw new Error('descriptor close failed');
          }
        } finally { native('AAsset_close', 'void', ['pointer'])(asset); }
        return row;
      }
      const rows = config.names.flatMap(name => [0, 1, 2, 3].map(mode => ({name, mode,
          stock: inspect(android, '', original, name, mode),
          replacement: inspect(implementation, 'e1_', replacement, name, mode)})));
      const metric = new NativeFunction(implementation.getExportByName('e1_metric'), 'uint64', ['uint']);
      return {schema: 'e1-native-asset-boundary.v2', cookie,
        scope: 'independent synthetic archive through real Android API; not engine namespace coverage',
        rows, metrics: {calls: metric(0).toString(), failures: metric(1).toString(),
          live_assets: metric(2).toString()}};
    } finally {
      try {
        if (manager && close) checked('close', method(63, 'void', ['pointer', 'pointer', 'pointer'])(
          environment, manager, close, ptr(0)));
      } finally {
        try {
          for (const reference of references.reverse()) method(23, 'void', ['pointer'])(environment, reference);
        } finally {
          if (attached === -2 && machineMethod(5, 'int', [])(machine) !== 0)
            throw new Error('DetachCurrentThread failed');
        }
      }
    }
  }
};
