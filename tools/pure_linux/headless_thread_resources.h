/* Independent boundary diagnostics. No syscall is added inside the step loop. */
#ifndef NASHURA_HEADLESS_THREAD_RESOURCES_H
#define NASHURA_HEADLESS_THREAD_RESOURCES_H

#include <stdint.h>
#include <stdio.h>
#include <sys/resource.h>
#include <time.h>

struct thread_resources {
    struct rusage usage;
    uint64_t cpu_ns, boundary_ns;
    int valid;
};

static struct thread_resources read_thread_resources(void) {
    struct thread_resources result = {0};
    struct timespec wall, cpu;
    result.valid = !clock_gettime(CLOCK_MONOTONIC, &wall)
        && !getrusage(RUSAGE_THREAD, &result.usage)
        && !clock_gettime(CLOCK_THREAD_CPUTIME_ID, &cpu);
    if (result.valid) {
        result.boundary_ns = (uint64_t)wall.tv_sec * UINT64_C(1000000000) + (uint64_t)wall.tv_nsec;
        result.cpu_ns = (uint64_t)cpu.tv_sec * UINT64_C(1000000000) + (uint64_t)cpu.tv_nsec;
    }
    return result;
}

static int64_t resource_time_us(struct timeval value) {
    return (int64_t)value.tv_sec * INT64_C(1000000) + value.tv_usec;
}

static int report_thread_resources(struct thread_resources before, struct thread_resources after,
                                  uint64_t loop_start, uint64_t loop_end) {
    struct timespec end;
    if (!before.valid || !after.valid || after.cpu_ns < before.cpu_ns
            || clock_gettime(CLOCK_MONOTONIC, &end)) return 0;
    uint64_t boundary_end = (uint64_t)end.tv_sec * UINT64_C(1000000000) + (uint64_t)end.tv_nsec;
    printf("HEADLESS_WARM_RESOURCES scope=diagnostic-only boundary_start_ns=%llu boundary_end_ns=%llu loop_start_ns=%llu loop_end_ns=%llu thread_cpu_ns=%llu user_us=%lld system_us=%lld minor_faults=%ld major_faults=%ld voluntary_switches=%ld involuntary_switches=%ld input_blocks=%ld output_blocks=%ld valid=1\n",
        (unsigned long long)before.boundary_ns, (unsigned long long)boundary_end,
        (unsigned long long)loop_start, (unsigned long long)loop_end,
        (unsigned long long)(after.cpu_ns - before.cpu_ns),
        (long long)(resource_time_us(after.usage.ru_utime) - resource_time_us(before.usage.ru_utime)),
        (long long)(resource_time_us(after.usage.ru_stime) - resource_time_us(before.usage.ru_stime)),
        after.usage.ru_minflt - before.usage.ru_minflt, after.usage.ru_majflt - before.usage.ru_majflt,
        after.usage.ru_nvcsw - before.usage.ru_nvcsw, after.usage.ru_nivcsw - before.usage.ru_nivcsw,
        after.usage.ru_inblock - before.usage.ru_inblock, after.usage.ru_oublock - before.usage.ru_oublock);
    return 1;
}

#endif
