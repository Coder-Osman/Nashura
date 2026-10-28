'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const classes = new Map();
const bodies = new Map();
const events = [];
let threadId = 1;
const preferences = 'android.app.SharedPreferencesImpl';
const editor = preferences + '$EditorImpl';
const application = 'com.supercell.titan.ApplicationUtilBase';
const game = 'com.supercell.titan.GameApp';
const loadingKey = 'LOADING_SCREEN_STATE_LABEL';

function getClass(name) {
  if (!classes.has(name)) {
    classes.set(name, new Proxy({}, { get: function (target, methodName) {
      if (!(methodName in target)) {
        const identity = name + '.' + String(methodName);
        target[methodName] = { overloads: [{ argumentTypes: [],
          apply: function (receiver, args) {
            const body = bodies.get(identity);
            return body ? body.apply(receiver, args) : undefined;
          } }] };
      }
      return target[methodName];
    } }));
  }
  return classes.get(name);
}

function invoke(name, methodName, args = []) {
  const method = getClass(name)[methodName].overloads[0];
  return method.implementation.apply({ $className: name }, args);
}

const identityMap = new Map();
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  Process: { id: 99, getCurrentThreadId: function () { return threadId; } },
  send: function (event) { events.push(event); },
  rpc: { exports: {} },
  Java: {
    perform: function (body) { body(); },
    backtrace: function () { return { frames: [] }; },
    use: function (name) {
      if (name === 'android.app.ActivityThread') {
        return { currentProcessName: function () { return 'nullsroyale.rel.free'; } };
      }
      if (name === 'android.os.Process') return { myUid: function () { return 10100; } };
      if (name === 'java.lang.String') return { $new: function (value) { return value; } };
      if (name === 'java.util.IdentityHashMap') {
        return { $new: function () { return {
          get: function (object) { return identityMap.get(object) ?? null; },
          put: function (object, value) { identityMap.set(object, value); },
          remove: function (object) { identityMap.delete(object); },
          size: function () { return identityMap.size; }
        }; } };
      }
      return getClass(name);
    }
  }
});
events.length = 0;

function entered(name) {
  return events.filter(function (event) {
    return event.event === 'call_enter' && event.details.method.startsWith(name + '(');
  });
}

const scenarios = {
  nested: function () {
    bodies.set(application + '.storeKeyValue', function () {
      invoke('c.a', 'n');
      invoke(game, 'requestBackup');
      invoke(editor, 'commit');
    });
    bodies.set('c.a.n', function () { invoke(editor, 'putString', ['encoded-key', 'encoded-value']); });
    invoke(application, 'storeKeyValue', [loadingKey, 'test']);
    for (const name of ['c.a.n', editor + '.putString', editor + '.commit', game + '.requestBackup']) {
      assert.equal(entered(name).length, 1, name);
      assert.equal(entered(name)[0].details.scope.scopeId, '1');
    }
    invoke(editor, 'commit');
    assert.equal(entered(editor + '.commit').length, 1);
  },
  exception: function () {
    bodies.set(application + '.storeKeyValue', function () { invoke('c.a', 'n'); });
    const sentinel = new Error('original exception');
    bodies.set('c.a.n', function () { throw sentinel; });
    assert.throws(function () { invoke(application, 'storeKeyValue', [loadingKey, 'test']); },
      function (error) { return error === sentinel; });
    invoke(editor, 'commit');
    assert.equal(entered(editor + '.commit').length, 0);
    assert.equal(events.filter(function (event) { return event.event === 'call_throw'; }).length, 2);
  },
  interleaved: function () {
    bodies.set(application + '.storeKeyValue', function () {
      threadId = 2;
      invoke(editor, 'commit');
      threadId = 1;
      invoke(editor, 'commit');
    });
    invoke(application, 'storeKeyValue', [loadingKey, 'test']);
    assert.equal(entered(editor + '.commit').length, 1);
    assert.equal(entered(editor + '.commit')[0].threadId, 1);
  },
  asynchronous: function () {
    const memoryResult = { $className: 'android.app.SharedPreferencesImpl$MemoryCommitResult' };
    let originalWrites = 0;
    bodies.set(application + '.storeKeyValue', function () {
      const result = invoke(editor, 'commitToMemory');
      invoke(preferences, 'enqueueDiskWrite', [result, null]);
    });
    bodies.set(editor + '.commitToMemory', function () { return memoryResult; });
    bodies.set(preferences + '.writeToFile', function () { originalWrites++; });
    invoke(application, 'storeKeyValue', [loadingKey, 'test']);
    threadId = 2;
    invoke(preferences, 'writeToFile', [memoryResult, false]);
    assert.equal(entered(preferences + '.writeToFile').length, 1);
    const disk = entered(preferences + '.writeToFile')[0];
    assert.equal(disk.threadId, 2);
    assert.equal(disk.details.scope.scopeId, '1');
    assert.equal(disk.details.scope.attribution, 'memory_commit_result_identity');
    assert.equal(identityMap.size, 0);
    invoke(preferences, 'writeToFile', [{ ...memoryResult }, false]);
    assert.equal(entered(preferences + '.writeToFile').length, 1);
    assert.equal(originalWrites, 2);
    assert.equal(events.filter(function (event) { return event.event === 'unattributed_disk_write'; }).length, 1);
    invoke(editor, 'commit');
    assert.equal(entered(editor + '.commit').length, 0);
  },
  disk_exception: function () {
    const memoryResult = {};
    bodies.set(application + '.storeKeyValue', function () { invoke(editor, 'commitToMemory'); });
    bodies.set(editor + '.commitToMemory', function () { return memoryResult; });
    bodies.set(preferences + '.writeToFile', function () { throw new Error('disk failure'); });
    invoke(application, 'storeKeyValue', [loadingKey, 'test']);
    threadId = 3;
    assert.throws(function () { invoke(preferences, 'writeToFile', [memoryResult]); }, /disk failure/);
    assert.equal(identityMap.size, 0);
    invoke(editor, 'commit');
    assert.equal(entered(editor + '.commit').length, 0);
  },
  read_scope: function () {
    bodies.set(application + '.getKeyValue', function () {
      invoke(preferences, 'getString', ['encoded-key', null]);
      return 'original';
    });
    assert.equal(invoke(application, 'getKeyValue', [loadingKey]), 'original');
    assert.equal(entered(preferences + '.getString').length, 1);
    invoke(application, 'getKeyValue', ['unrelated-key']);
    assert.equal(entered(preferences + '.getString').length, 1);
  },
  association_limit: function () {
    bodies.set(application + '.storeKeyValue', function () { invoke(editor, 'commitToMemory'); });
    bodies.set(editor + '.commitToMemory', function () { return {}; });
    for (let index = 0; index < 257; index++) invoke(application, 'storeKeyValue', [loadingKey, 'test']);
    assert.equal(identityMap.size, 256);
    assert.equal(events.filter(function (event) { return event.event === 'disk_commit_untracked'; }).length, 1);
  },
  observer_failure: function () {
    const memoryResult = {};
    bodies.set(application + '.storeKeyValue', function () { return invoke(editor, 'commitToMemory'); });
    bodies.set(editor + '.commitToMemory', function () { return memoryResult; });
    identityMap.set = function () { throw new Error('observer failure'); };
    assert.equal(invoke(application, 'storeKeyValue', [loadingKey, 'test']), memoryResult);
    assert.equal(events.filter(function (event) { return event.event === 'observer_error'; }).length, 1);
    invoke(editor, 'commit');
    assert.equal(entered(editor + '.commit').length, 0);
  }
};
assert.ok(process.argv[3] in scenarios);
scenarios[process.argv[3]]();
process.stdout.write('MOCK_SCOPE_PASS ' + process.argv[3] + '\n');
