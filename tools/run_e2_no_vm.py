"""Run the pinned native preinit probe; a load is never a battle pass."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time


def run(config_path, probe, guest_probe, output):
    import frida

    config = json.loads(config_path.read_text())
    output.mkdir(parents=True, exist_ok=False)
    tools = Path(__file__).parent
    scripts = {}
    finished = threading.Event()
    detached = {}
    def save(name, value):
        (output / name).write_text(json.dumps(value, indent=2) + '\n')
    def adb(*arguments):
        result = subprocess.run(config['adb'] + list(arguments), capture_output=True, text=True, timeout=180)
        with (output / 'commands.jsonl').open('a') as target:
            target.write(json.dumps(dict(argv=config['adb'] + list(arguments), code=result.returncode, stdout=result.stdout, stderr=result.stderr)) + '\n')
        result.check_returncode()
        return result.stdout.strip()
    if adb('emu', 'avd', 'name').splitlines()[0] != config['avd']:
        raise RuntimeError('AVD mismatch')
    def identity():
        raw = adb('shell', 'sha256sum', *config['guest_identity'].keys())
        actual = {line.split(maxsplit=1)[1].strip(): line.split()[0] for line in raw.splitlines()}
        if actual != config['guest_identity']:
            raise RuntimeError('input identity differs')
        return actual
    save('identity-before.json', identity())
    adb('push', str(probe), guest_probe)
    adb('shell', 'chmod', '755', guest_probe)
    expected = hashlib.sha256(probe.read_bytes()).hexdigest()
    if adb('shell', 'sha256sum', guest_probe).split()[0] != expected:
        raise RuntimeError('probe transfer identity differs')
    library = next(path for path in config['guest_identity'] if path.endswith('/libg.so'))
    device = frida.get_device_manager().add_remote_device('127.0.0.1:' + str(config['frida_port']))
    def native_output(pid, descriptor, data):
        with (output / ('native-' + str(descriptor) + '.txt')).open('ab') as target:
            target.write(data)
    device.on('output', native_output)
    pid = device.spawn([guest_probe, library, '--pinned-preinit', '--wait-for-attachment'], env={'LD_LIBRARY_PATH': str(Path(library).parent)}, stdio='pipe')
    save('owned-process.json', dict(pid=pid, avd=config['avd'], probe_sha256=expected, stat=adb('shell', 'cat', '/proc/' + str(pid) + '/stat')))
    session = device.attach(pid)
    def on_detached(reason, crash):
        detached.update(reason=reason, crash=None if crash is None else dict(summary=crash.summary, report=crash.report))
        save('detached.json', detached)
        finished.set()
    session.on('detached', on_detached)
    device.resume(pid)
    for attempt in range(60):
        stdout = output / 'native-1.txt'
        if stdout.exists() and 'waiting_for_instrumentation' in stdout.read_text():
            break
        if finished.is_set():
            save('result.json', dict(status='blocked', reason='native load failed before instrumentation', no_vm_verified=False))
            return 2
        time.sleep(1)
    else:
        raise RuntimeError('native load readiness not observed')
    (output / 'maps-before-preinit.txt').write_text(adb('shell', 'cat', '/proc/' + str(pid) + '/maps'))
    def create(name, filename, settings):
        script = session.create_script((tools / filename).read_text())
        def message(payload, data):
            with (output / 'messages.jsonl').open('a') as target:
                target.write(json.dumps(dict(script=name, payload=payload)) + '\n')
        script.on('message', message)
        script.load()
        script.exports_sync.configure(settings)
        scripts[name] = script
    observer = json.loads(Path(config['observer_config']).read_text())
    observer['functions'] = [row for row in observer['functions'] if row['name'] not in {'PreferenceNativeReadBridge', 'PreferenceNativeWriteBridge'}]
    observer.update(capture_backtraces=False, enumerate_threads=False)
    observer['stream_native'] = ['JNIEnvAttach', 'JNIClassResolve', 'JNIExceptionCheck']
    create('e0', 'e0_dependency_observer.js', observer)
    create('e2', 'e2_jni_config_observer.js', observer)
    resource = dict(config['resource_config'], audit_path=config['audit_prefix'] + output.name + '.tsv')
    create('resource', 'e1_resource_redirect.js', resource)
    create('fmod', 'e1_fmod_binding.js', config['fmod_config'])
    replacement = dict(config['replacement'], mode='strict-targeted', stream_events=True)
    create('replacement', 'e2_config_replace.js', replacement)
    adb('shell', 'kill', '-USR1', str(pid))
    print('no-VM native probe resumed ' + str(pid), flush=True)
    finished.wait(120)
    if not finished.is_set():
        for name, script in scripts.items():
            save(name + '-partial.json', script.exports_sync.snapshot())
        save('result.json', dict(status='blocked', reason='probe remains running after observation window; not killed', pid=pid, no_vm_verified=False))
        return 2
    save('identity-after.json', identity())
    save('result.json', dict(status='blocked', reason=detached.get('reason'),
                             no_vm_verified=False, android_free_verified=False, complete_battle_verified=False))
    device.off('output', native_output)
    print(json.dumps(detached), flush=True)
    return 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--guest-probe', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.config, args.probe, args.guest_probe, args.output))
