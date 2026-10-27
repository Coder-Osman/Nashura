#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <errno.h>

typedef int (*Dispatch)(void *, void *, void *);

static pthread_once_t initialized = PTHREAD_ONCE_INIT;
static pthread_mutex_t mutex;
static int initialization_error;
static _Atomic uint64_t counters[6];

static void initialize(void) {
    pthread_mutexattr_t attributes;
    initialization_error = pthread_mutexattr_init(&attributes);
    if (initialization_error) return;
    initialization_error = pthread_mutexattr_settype(&attributes, PTHREAD_MUTEX_ERRORCHECK);
    if (!initialization_error) initialization_error = pthread_mutex_init(&mutex, &attributes);
    int destroyed = pthread_mutexattr_destroy(&attributes);
    if (!initialization_error) initialization_error = destroyed;
}

int e21_dispatch_gate(void *key, void *value, void *source, Dispatch callback) {
    if (!callback || pthread_once(&initialized, initialize) || initialization_error) return -12;
    atomic_fetch_add(&counters[0], 1);
    uint64_t waiting = atomic_fetch_add(&counters[3], 1) + 1;
    uint64_t peak = atomic_load(&counters[4]);
    while (waiting > peak && !atomic_compare_exchange_weak(&counters[4], &peak, waiting)) {}
    int locked = pthread_mutex_lock(&mutex);
    atomic_fetch_sub(&counters[3], 1);
    if (locked) {
        atomic_fetch_add(&counters[5], 1);
        return locked == EDEADLK ? -11 : -12;
    }
    atomic_fetch_add(&counters[2], 1);
    int result = callback(key, value, source);
    atomic_fetch_sub(&counters[2], 1);
    atomic_fetch_add(&counters[1], 1);
    if (pthread_mutex_unlock(&mutex)) return -12;
    return result;
}

uint64_t e21_dispatch_gate_stat(unsigned index) {
    return index < 6 ? atomic_load(&counters[index]) : UINT64_MAX;
}
