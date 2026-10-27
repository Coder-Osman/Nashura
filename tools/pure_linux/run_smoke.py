"""Run only an independently built synthetic Linux ELF, never the game."""
import argparse
import hashlib
import json
import platform
import subprocess
import tempfile
from pathlib import Path

EXPECTED = ['process_syscall', 'monotonic_clock', 'file_io', 'unix_socket_ipc',
            'clone_thread_shared_state', 'signal_delivery', 'all_checks']

def validate(stdout, returncode, expected_exit=0):
    records = [json.loads(line) for line in stdout.splitlines()]
    return (returncode == expected_exit and
            records == [{'check': name, 'pass': True} for name in EXPECTED])

def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--qemu-sha256', required=True)
    parser.add_argument('--elf-sha256', required=True)
    parser.add_argument('--expected-exit', type=int, default=0)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        parser.error('requires Linux x86_64 host')
    for path, expected in [(args.qemu,args.qemu_sha256),(args.elf,args.elf_sha256)]:
        if digest(path) != expected:
            parser.error('input SHA mismatch')
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='smoke-', dir=args.output) as tmp:
        p = subprocess.run([str(args.qemu.resolve()), str(args.elf.resolve())],
                           cwd=tmp, capture_output=True, timeout=20)
        output = p.stdout.decode('utf-8')
        file_ok = (Path(tmp)/'smoke-file.txt').read_bytes() == b'Nashura file canary\0'
        passed = validate(output, p.returncode, args.expected_exit) and file_ok
        (args.output/'stdout.jsonl').write_bytes(p.stdout)
        (args.output/'stderr.txt').write_bytes(p.stderr)
        report = {'schema':'nashura-linux-smoke.v1', 'synthetic':True,
                  'native_game_verified':False, 'status':'PURE_LINUX_SMOKE_PASS' if passed else 'FAIL',
                  'host_system':platform.system(), 'host_arch':platform.machine(),
                  'kernel':platform.release(), 'qemu_version':subprocess.check_output(
                      [str(args.qemu.resolve()),'--version'],text=True).splitlines()[0],
                  'qemu_sha256':args.qemu_sha256, 'elf_sha256':args.elf_sha256,
                  'exit_code':p.returncode, 'expected_exit_code':args.expected_exit,
                  'stdout_sha256':hashlib.sha256(p.stdout).hexdigest(),
                  'stderr_sha256':hashlib.sha256(p.stderr).hexdigest(), 'file_canary_verified':file_ok,
                  'checks':EXPECTED}
        (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
        return 0 if passed else 1

if __name__ == '__main__':
    raise SystemExit(main())
