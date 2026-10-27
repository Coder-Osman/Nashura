#include <dlfcn.h>
#include <pthread.h>
#include <stdio.h>
#include <time.h>

static _Thread_local int tls_canary = 17;

static void *thread_entry(void *argument) {
    int *result = argument;
    *result = tls_canary == 17;
    tls_canary = 31;
    return NULL;
}

int main(int argument_count, char **arguments) {
    pthread_t thread;
    int child_ok = 0;
    struct timespec instant;
    if (clock_gettime(CLOCK_MONOTONIC, &instant) != 0) return 2;
    if (pthread_create(&thread, NULL, thread_entry, &child_ok) != 0) return 3;
    if (pthread_join(thread, NULL) != 0 || !child_ok || tls_canary != 17) return 4;
    puts("BIONIC_SYNTHETIC_ABI_ONLY");
    if (argument_count == 2) {
        void *handle = dlopen(arguments[1], RTLD_NOW | RTLD_LOCAL);
        if (!handle) {
            fprintf(stderr, "LOAD_FAILED: %s\n", dlerror());
            return 5;
        }
        puts("LOAD_ONLY: no explicit lifecycle calls; constructors may have run");
        if (dlclose(handle) != 0) return 6;
    }
    return 0;
}
