#include <stdio.h>
#include <stdint.h>
#include <time.h>
static uint64_t now_ns(void) {
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return (uint64_t)value.tv_sec * 1000000000ULL + value.tv_nsec;
}
int main(void) {
    const int iterations = 1000000;
    for (int trial = 0; trial < 5; ++trial) {
        uint64_t total = 0, start = now_ns();
        for (int index = 0; index < iterations; ++index) {
            uint64_t before = now_ns();
            uint64_t elapsed = now_ns() - before;
            total += elapsed;
        }
        uint64_t end = now_ns();
        printf("trial=%d iterations=%d wall_ns=%llu measured_empty_total_ns=%llu",
               trial, iterations, (unsigned long long)(end-start), (unsigned long long)total);
        putchar(10);
    }
    return 0;
}
