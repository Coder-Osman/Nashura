"""Lifecycle for one task-owned COW-cloned Android engine; no shared resets."""
from pathlib import Path
import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
BASE = ROOT.with_name('standalone-engine-20260926')
FROZEN = ROOT.with_name('standalone-micro-benchmark-20260927')
SDK = Path('/Users/gujiga/Library/Android/sdk')
ADB = SDK / 'platform-tools/adb'
NAME, SERIAL, PORT = 'hog-tree-native-20260929', 'emulator-5594', 28994
APP = 'nullsroyale.rel.free'
LOGS = ROOT / 'logs'
AVD_HOME = ROOT / 'avd-flat'


def save(name, value):
    path = LOGS / name
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    tmp.replace(path)


def command(argv, timeout=30):
    return subprocess.run([str(v) for v in argv], check=True, capture_output=True,
                          text=True, timeout=timeout).stdout.strip()


def adb(*args, timeout=30):
    return command([ADB, '-s', SERIAL, *args], timeout)


def identity(pid):
    try:
        return {'start': command(['ps', '-p', pid, '-o', 'lstart=']),
                'command': command(['ps', '-p', pid, '-o', 'command='])}
    except subprocess.CalledProcessError:
        return None


def owned_live(record):
    current = identity(record.get('pid', 0))
    return bool(current and current['start'] == record.get('identity', {}).get('start')
                and '-avd ' + NAME in current['command'])


def request(text):
    with socket.create_connection(('127.0.0.1', PORT), timeout=3) as sock:
        sock.settimeout(10)
        sock.sendall((text + '\n').encode())
        value = json.loads(sock.makefile('rb').readline())
    if not value.get('ok'):
        raise RuntimeError(value)
    return value


def record_read():
    path = LOGS / 'owner.json'
    return json.loads(path.read_text()) if path.exists() else {}


def prepare():
    source = BASE / 'avd/standalone-source.avd'
    destination = AVD_HOME / (NAME + '.avd')
    if destination.exists():
        if not (LOGS / 'clone-flat.json').exists():
            raise RuntimeError('incomplete clone requires inspection')
        return
    processes = command(['ps', '-axo', 'pid,command'])
    if any('-avd standalone-source' in line for line in processes.splitlines()):
        raise RuntimeError('source AVD is running')
    if shutil.disk_usage(ROOT).free < 20 * 2**30:
        raise RuntimeError('less than 20 GiB disk reserve')
    destination.mkdir(parents=True)
    files = []
    for path in source.iterdir():
        if path.is_file() and (path.name == 'config.ini' or path.name.endswith('.img')):
            overlay = path.with_name(path.name + '.qcow2')
            if overlay.exists():
                command([SDK / 'emulator/qemu-img', 'convert', '-f', 'qcow2', '-O', 'raw',
                         overlay, destination / path.name], timeout=180)
            else:
                command(['/bin/cp', '-c', path, destination / path.name], timeout=60)
            files.append({'name': path.name, 'bytes': path.stat().st_size,
                          'source_mtime_ns': path.stat().st_mtime_ns})
    (AVD_HOME / (NAME + '.ini')).write_text(
        'avd.ini.encoding=UTF-8\npath=' + str(destination) + '\ntarget=android-30\n')
    save('clone-flat.json', {'source': str(source), 'destination': str(destination),
                        'method': 'flatten frozen qcow2 into independent raw disk',
                        'reason': 'emulator QEMU-version migration discards an old qcow2 overlay',
                        'files': files, 'created': time.time()})


def stop():
    record = record_read()
    if not owned_live(record):
        return {'stopped': False, 'owned_process_live': False}
    if adb('emu', 'avd', 'name').splitlines()[0] != NAME:
        raise RuntimeError('AVD name differs from task ownership')
    forwards = command([ADB, 'forward', '--list'])
    if f'{SERIAL} tcp:{PORT} ' in forwards:
        adb('forward', '--remove', f'tcp:{PORT}')
    adb('emu', 'kill')
    deadline = time.monotonic() + 30
    while owned_live(record) and time.monotonic() < deadline:
        time.sleep(.2)
    if owned_live(record):
        raise RuntimeError('owned AVD did not exit')
    record.update(stopped=time.time(), ready=False)
    save('owner.json', record)
    result = {'stopped': True, 'pid': record['pid'], 'owned_process_live': False,
              'devices_after': command([ADB, 'devices', '-l']),
              'forwards_after': command([ADB, 'forward', '--list'])}
    save('cleanup.json', result)
    return result


def start():
    record = record_read()
    if owned_live(record):
        if not record.get('ready') and request('status').get('coldReady'):
            return finish_start(record)
        return {'already_running': True, 'owner': record, 'status': request('status')}
    prepare()
    for port in (5594, 5595, PORT):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', port))
    if SERIAL in command([ADB, 'devices']):
        raise RuntimeError('task serial is already in use')
    argv = [SDK / 'emulator/emulator', '-avd', NAME, '-port', '5594',
            '-cores', '2', '-memory', '2048', '-no-window', '-no-audio',
            '-no-boot-anim', '-no-snapshot', '-gpu', 'swiftshader_indirect', '-no-metrics']
    with (LOGS / 'avd.log').open('a') as output:
        proc = subprocess.Popen([str(v) for v in argv], stdout=output, stderr=output,
                                start_new_session=True,
                                env={**os.environ, 'ANDROID_AVD_HOME': str(AVD_HOME)})
    time.sleep(.1)
    record = {'pid': proc.pid, 'identity': identity(proc.pid), 'argv': [str(v) for v in argv],
              'started': time.time(), 'serial': SERIAL, 'port': PORT, 'ready': False}
    save('owner.json', record)
    adb('wait-for-device', timeout=90)
    if adb('emu', 'avd', 'name').splitlines()[0] != NAME:
        raise RuntimeError('wrong task AVD')
    adb('root')
    adb('wait-for-device', timeout=30)
    deadline = time.monotonic() + 90
    while adb('shell', 'getprop', 'sys.boot_completed') != '1':
        if time.monotonic() > deadline:
            raise RuntimeError('boot timeout')
        time.sleep(.5)
    # Guest-only isolation: no firewall or environment changes on the host.
    firewall = {}
    for family in ('iptables', 'ip6tables'):
        chain = 'CR_TREE_SEARCH'
        adb('shell', family, '-N', chain)
        adb('shell', family, '-A', chain, '-o', 'lo', '-j', 'RETURN')
        adb('shell', family, '-A', chain, '-j', 'REJECT')
        adb('shell', family, '-I', 'OUTPUT', '1', '-j', chain)
        firewall[family] = adb('shell', family, '-S', chain)
    adb('shell', 'am', 'force-stop', APP)
    apk = adb('shell', 'pm', 'path', APP).removeprefix('package:')
    if not apk.startswith('/data/app/') or '\n' in apk:
        raise RuntimeError('unexpected offline APK path')
    libdir = str(Path(apk).parent / 'lib/arm64')
    libg = adb('shell', 'sha256sum', libdir + '/libg.so').split()[0]
    if libg != '110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783':
        raise RuntimeError('unexpected original engine')
    probe = FROZEN / 'libreference.so'
    expected = json.loads((FROZEN / 'build-manifest.json').read_text())['outputs']['libreference.so']
    if hashlib.sha256(probe.read_bytes()).hexdigest() != expected:
        raise RuntimeError('frozen probe changed')
    adb('push', str(probe), libdir + '/libcrprobe.so')
    if adb('shell', 'sha256sum', libdir + '/libcrprobe.so').split()[0] != expected:
        raise RuntimeError('installed probe identity differs')
    adb('forward', f'tcp:{PORT}', 'tcp:26789')
    adb('shell', 'am', 'start', '--user', '0', '-f', '0x18000000', '-n',
        APP + '/com.supercell.clashroyale.Engine0App')
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            status = request('status')
            if status.get('coldReady'):
                break
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(.3)
    else:
        raise RuntimeError('engine startup timeout')
    return finish_start(record)


def finish_start(record):
    attestation = request('attest')['attestation']
    reference = json.loads((ROOT.with_name('standalone-resident-20260927') / 'logs/api-android.json').read_text())['identity']
    for key in ('libg_sha256', 'probe_sha256', 'content_fingerprint_sha256', 'content_assets_sha256'):
        if attestation[key] != reference[key]:
            raise RuntimeError('frozen runtime identity differs: ' + key)
    record.update(ready=True, app_pid=adb('shell', 'pidof', APP + ':engine0'),
                  identity=identity(record['pid']))
    save('owner.json', record)
    firewall = {family: adb('shell', family, '-S', 'CR_TREE_SEARCH')
                for family in ('iptables', 'ip6tables')}
    result = {'owner': record, 'attestation': attestation, 'firewall': firewall,
              'host': command(['uname', '-a']), 'memory_pressure': command(['memory_pressure', '-Q'])}
    save('runtime.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'stop', 'status'))
    args = parser.parse_args()
    LOGS.mkdir(exist_ok=True)
    with (LOGS / 'lifecycle.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'start':
            result = start()
        elif args.action == 'stop':
            result = stop()
        else:
            record = record_read()
            result = {'owner': record, 'owned_process_live': owned_live(record)}
        print(json.dumps(result, ensure_ascii=False), flush=True)
