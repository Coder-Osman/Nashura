"""Attach after the disposable probe's native-init-gdb stop marker.

Use gdb -q -batch -p PID -x THIS_FILE, without a concurrent strace tracer.
The launcher checks the exact engine SHA. This script observes the first stop
in libg and terminates that disposable inferior; it never changes engine state.
"""
import gdb
from pathlib import Path

gdb.execute('set pagination off')
gdb.execute('set confirm off')
gdb.execute('set print thread-events off')
gdb.execute('handle SIGSTOP nostop noprint pass')
gdb.execute('handle SIGCONT nostop noprint pass')
gdb.execute('handle SIG37 nostop noprint pass')
gdb.execute('handle SIG40 nostop noprint pass')
gdb.execute('handle SIGSEGV stop print pass')
inferior = gdb.selected_inferior()
rows = Path('/proc/%d/maps' % inferior.pid).read_text().splitlines()
engine_maps = [line.split(None, 5) for line in rows
               if line.split(None, 5)[-1].endswith('/libg.so')]
# Separate RELRO/load mappings need not share start-offset. The matching ELF
# has a PT_LOAD at file offset and virtual address zero; use that mapping only.
bases = {int(row[0].split('-')[0], 16) for row in engine_maps if int(row[2], 16) == 0}
if len(bases) != 1:
    print('NASHURA_GDB_ENGINE_MAPS=' + repr(engine_maps))
    raise RuntimeError('missing or ambiguous zero-offset mapping')
base = bases.pop()
print('NASHURA_GDB_ENGINE_FOUND=1')
gdb.execute('signal SIGCONT')
for attempt in range(100):
    if not gdb.selected_inferior().pid:
        print('NASHURA_GDB_TARGET_EXITED')
        break
    pc = int(gdb.parse_and_eval('$pc'))
    print('NASHURA_GDB_STOP_PC=0x%x' % pc)
    if base <= pc < base + 0x2000000:
        print('NASHURA_GDB_LIBG_STOP_OFFSET=0x%x' % (pc - base))
        gdb.execute('bt 40')
        gdb.execute('info registers x0 x1 x2 x3 x8 x19 x20 x21 x22 x29 x30 sp pc')
        gdb.execute('x/12i $pc-24')
        for slot in [0x19ad878, 0x1a06ab8]:
            gdb.execute('x/gx 0x%x' % (base + slot))
        frame = int(gdb.parse_and_eval('$x29'))
        for index in range(24):
            if not frame or frame & 15: break
            try:
                data = bytes(gdb.selected_inferior().read_memory(frame & ((1 << 56) - 1), 16))
            except gdb.MemoryError:
                break
            previous = int.from_bytes(data[:8], 'little')
            lr = int.from_bytes(data[8:], 'little')
            if base <= lr < base + 0x2000000:
                print('NASHURA_GDB_FRAME_POINTER_LR_%d=0x%x' % (index, lr - base))
            if previous <= frame or previous - frame > 16 * 1024 * 1024: break
            frame = previous
        gdb.execute('kill')
        break
    gdb.execute('continue')
