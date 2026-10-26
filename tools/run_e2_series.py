"""Bounded E2 runs, retaining each attempt and only stopping its owned PID."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


def release_owned(config, previous):
    adb = config['adb']
    def read(*arguments):
        return subprocess.check_output(adb + list(arguments), text=True, timeout=30).strip()
    if read('emu', 'avd', 'name').splitlines()[0] != config['avd']:
        raise RuntimeError('AVD identity differs')
    owned = json.loads((previous / 'owned-process.json').read_text())
    pid = owned['pid']
    active = read('shell', 'pidof ' + config['package'] + ':engine0 || true')
    if not active:
        return
    if active != str(pid):
        raise RuntimeError('active process is not the owned attempt')
    stat = read('shell', 'cat', '/proc/' + str(pid) + '/stat')
    if stat.split(') ', 1)[1].split()[19] != owned['stat'].split(') ', 1)[1].split()[19]:
        raise RuntimeError('PID reused')
    with (previous / 'cleanup.json').open('x') as target:
        json.dump(dict(pid=pid, stat=stat, action='force-stop completed owned attempt'), target)
    subprocess.run(adb + ['shell', 'am', 'force-stop', config['package']], check=True)
    if read('shell', 'pidof ' + config['package'] + ':engine0 || true'):
        raise RuntimeError('owned process still alive')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--previous', type=Path, required=True)
    parser.add_argument('--modes', nargs='+', required=True, choices=['original', 'replacement', 'strict-targeted', 'shadow'])
    parser.add_argument('--prefix', required=True)
    args = parser.parse_args()
    base = json.loads(args.config.read_text())
    previous = args.previous
    for index, mode in enumerate(args.modes, 1):
        release_owned(base, previous)
        config = dict(base, mode=mode, replacement=dict(base['replacement'], mode=mode))
        if mode == 'original':
            config.pop('replacement')
        path = args.root / (args.prefix + '-' + str(index) + '-' + mode)
        config_path = path.with_suffix('.config.json')
        with config_path.open('x') as target:
            json.dump(config, target, indent=2)
        command = [sys.executable, '-u', str(Path(__file__).with_name('run_e2_observed.py')),
                   '--config', str(config_path), '--output', str(path)]
        with path.with_suffix('.stdout').open('x') as output:
            result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT)
        print(json.dumps(dict(attempt=path.name, command=command, exit_code=result.returncode)), flush=True)
        if result.returncode:
            raise SystemExit(result.returncode)
        previous = path
    release_owned(base, previous)


if __name__ == '__main__':
    main()
