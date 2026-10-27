from pathlib import Path
import shutil
import subprocess


def test_native_gate_serializes_callbacks_and_reports_recursive_entry(tmp_path):
    compiler = shutil.which('cc')
    assert compiler, 'existing C compiler required'
    source = tmp_path / 'gate-test.c'
    source.write_text(r'''
#include <assert.h>
#include <unistd.h>
#include "e2_1_pn_dispatch_gate.c"
static _Atomic int running;
static int callback(void *key, void *value, void *source) {
    assert(key == (void *)1 && value == (void *)2 && source == (void *)3);
    assert(atomic_fetch_add(&running, 1) == 0);
    usleep(1000);
    assert(e21_dispatch_gate(key, value, source, callback) == -11);
    assert(atomic_fetch_sub(&running, 1) == 1);
    return 37;
}
static void *worker(void *argument) {
    (void)argument;
    for (int iteration = 0; iteration < 8; ++iteration)
        assert(e21_dispatch_gate((void *)1, (void *)2, (void *)3, callback) == 37);
    return NULL;
}
int main(void) {
    assert(e21_dispatch_gate(NULL, NULL, NULL, NULL) == -12);
    pthread_t workers[2];
    for (int index = 0; index < 2; ++index) assert(pthread_create(&workers[index], NULL, worker, NULL) == 0);
    for (int index = 0; index < 2; ++index) assert(pthread_join(workers[index], NULL) == 0);
    assert(e21_dispatch_gate_stat(0) == 32);
    assert(e21_dispatch_gate_stat(1) == 16);
    assert(e21_dispatch_gate_stat(2) == 0 && e21_dispatch_gate_stat(3) == 0);
    assert(e21_dispatch_gate_stat(4) >= 1 && e21_dispatch_gate_stat(5) == 16);
    assert(e21_dispatch_gate_stat(6) == UINT64_MAX);
    return 0;
}
''')
    executable = tmp_path / 'gate-test'
    subprocess.run([compiler, '-std=c11', '-D_DEFAULT_SOURCE', '-Wall', '-Wextra', '-Werror', '-pthread',
                    '-I', str(Path(__file__).parents[1]), str(source), '-o', str(executable)], check=True,
                   capture_output=True, text=True, timeout=30)
    subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=10)
