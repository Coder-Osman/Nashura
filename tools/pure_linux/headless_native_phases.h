/* Diagnostic-only native call-site attribution for the pinned ARM64 build.
 * No function body is reimplemented. Stubs call the exact original target.
 * Never enable in a benchmark or combine with native optimization candidates.
 */
#ifndef NASHURA_HEADLESS_NATIVE_PHASES_H
#define NASHURA_HEADLESS_NATIVE_PHASES_H

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include "headless_action_distributions.h"

enum native_phase { NP_STATE, NP_WORLD, NP_OBJECTS, NP_QUEUE, NP_APPLY_2, NP_APPLY_3,
    NP_ARRAY_NEW, NP_ARRAY_DELETE, NP_COMPONENT, NP_COUNT };
static const char *const native_phase_names[] = {
    "game_state_update", "world_update", "object_collection_tick",
    "command_queue", "command_dispatch_mode2", "command_dispatch_mode3",
    "object_array_new", "object_array_delete", "component_update"
};
static struct native_phase_stat {
    uint64_t calls, inclusive_ns, exclusive_ns, min_ns, max_ns, zero_results, zero_duration_calls;
    uintptr_t first_target;
    uint64_t different_target_calls;
    uint64_t requested_bytes, max_request_bytes;
} native_phase_stats[NP_COUNT];
static struct native_phase_frame { enum native_phase phase; uint64_t started, children; }
    native_phase_stack[32];
static unsigned native_phase_depth;
static int native_phases_installed, native_phase_failed;
static uintptr_t native_phase_engine;

static uint64_t native_phase_clock(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) { native_phase_failed = 1; return 0; }
    return (uint64_t)now.tv_sec * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
}
static void native_phase_begin(enum native_phase phase, uintptr_t target) {
    if (native_phase_depth >= 32) { native_phase_failed = 1; _exit(126); }
    struct native_phase_stat *stat = &native_phase_stats[phase];
    if (!stat->calls) stat->first_target = target;
    else if (stat->first_target != target) ++stat->different_target_calls;
    native_phase_stack[native_phase_depth++] =
        (struct native_phase_frame){ phase, native_phase_clock(), 0 };
}
static uint64_t native_phase_end(enum native_phase phase, int result) {
    uint64_t ended = native_phase_clock();
    if (!native_phase_depth) { native_phase_failed = 1; _exit(126); }
    struct native_phase_frame frame = native_phase_stack[--native_phase_depth];
    if (frame.phase != phase || ended < frame.started || ended - frame.started < frame.children) {
        native_phase_failed = 1; _exit(126);
    }
    uint64_t elapsed = ended - frame.started;
    struct native_phase_stat *stat = &native_phase_stats[phase];
    if (!stat->calls || elapsed < stat->min_ns) stat->min_ns = elapsed;
    if (elapsed > stat->max_ns) stat->max_ns = elapsed;
    ++stat->calls;
    stat->inclusive_ns += elapsed;
    stat->exclusive_ns += elapsed - frame.children;
    if (result == 0) ++stat->zero_results;
    if (elapsed == 0) ++stat->zero_duration_calls;
    if (native_phase_depth) native_phase_stack[native_phase_depth - 1].children += elapsed;
    return elapsed;
}
static void native_phase_reset(void) {
    memset(native_phase_stats, 0, sizeof(native_phase_stats));
    native_phase_depth = 0;
    native_phase_failed = 0;
}
static void native_phase_report(void) {
    if (!native_phases_installed) return;
    for (unsigned i = 0; i < NP_COUNT; ++i) {
        struct native_phase_stat *s = &native_phase_stats[i];
        printf("HEADLESS_NATIVE_PHASE scope=diagnostic-only phase=%s calls=%llu inclusive_ns=%llu exclusive_ns=%llu min_ns=%llu max_ns=%llu zero_results=%llu zero_duration_calls=%llu first_target_offset=0x%llx different_target_calls=%llu requested_bytes=%llu max_request_bytes=%llu valid=%d\n",
            native_phase_names[i], (unsigned long long)s->calls,
            (unsigned long long)s->inclusive_ns, (unsigned long long)s->exclusive_ns,
            (unsigned long long)s->min_ns, (unsigned long long)s->max_ns,
            (unsigned long long)s->zero_results,
            (unsigned long long)s->zero_duration_calls,
            (unsigned long long)(s->first_target ? s->first_target - native_phase_engine : 0),
            (unsigned long long)s->different_target_calls,
            (unsigned long long)s->requested_bytes, (unsigned long long)s->max_request_bytes,
            !native_phase_failed && !native_phase_depth);
    }
    fflush(stdout);
}

typedef void (*native_one_fn)(void *);
typedef void (*native_tick_fn)(void *, int32_t);
typedef int32_t (*native_apply_fn)(void *, void *, int32_t, int32_t);

static __attribute__((noinline)) void native_state_phase(void *state) {
    uintptr_t target = native_phase_engine + 0x1062274;
    native_phase_begin(NP_STATE, target);
    ((native_one_fn)target)(state);
    native_phase_end(NP_STATE, 0);
}
static __attribute__((noinline)) void native_world_phase(void *world) {
    uintptr_t target = native_phase_engine + 0xcef550;
    native_phase_begin(NP_WORLD, target);
    ((native_one_fn)target)(world);
    native_phase_end(NP_WORLD, 0);
}
static __attribute__((noinline)) void native_objects_phase(void *objects, int32_t tick,
        uintptr_t unused2, uintptr_t unused3, uintptr_t target) {
    (void)unused2; (void)unused3;
    native_phase_begin(NP_OBJECTS, target);
    ((native_tick_fn)target)(objects, tick);
    native_phase_end(NP_OBJECTS, 0);
}
static __attribute__((noinline)) void native_queue_phase(void *queue) {
    uintptr_t target = native_phase_engine + 0xd2f37c;
    native_phase_begin(NP_QUEUE, target);
    ((native_one_fn)target)(queue);
    native_phase_end(NP_QUEUE, 0);
}
static __attribute__((noinline)) int32_t native_apply2_phase(void *command, void *state,
        int32_t mode, int32_t flag, uintptr_t target) {
    if (mode != 2 || flag != 0) { native_phase_failed = 1; _exit(126); }
    int32_t tick = action_samples ? ((int32_t (*)(void *))(native_phase_engine + 0x1062bc0))(state) : -1;
    native_phase_begin(NP_APPLY_2, target);
    int32_t result = ((native_apply_fn)target)(command, state, mode, flag);
    uint64_t elapsed = native_phase_end(NP_APPLY_2, result);
    action_sample_add(AS_MODE2, elapsed, target - native_phase_engine, -1, tick, result, -1, -1);
    return result;
}
static __attribute__((noinline)) int32_t native_apply3_phase(void *command, void *state,
        int32_t mode, int32_t flag, uintptr_t target) {
    if (mode != 3 || flag != 0) { native_phase_failed = 1; _exit(126); }
    int32_t tick = action_samples ? ((int32_t (*)(void *))(native_phase_engine + 0x1062bc0))(state) : -1;
    native_phase_begin(NP_APPLY_3, target);
    int32_t result = ((native_apply_fn)target)(command, state, mode, flag);
    uint64_t elapsed = native_phase_end(NP_APPLY_3, result);
    action_sample_add(AS_MODE3, elapsed, target - native_phase_engine, -1, tick, result, -1, -1);
    return result;
}
static __attribute__((noinline)) void *native_array_new_phase(size_t bytes) {
    uintptr_t target = native_phase_engine + 0x17e4d60;
    struct native_phase_stat *stat = &native_phase_stats[NP_ARRAY_NEW];
    stat->requested_bytes += bytes;
    if (bytes > stat->max_request_bytes) stat->max_request_bytes = bytes;
    native_phase_begin(NP_ARRAY_NEW, target);
    void *result = ((void *(*)(size_t))target)(bytes);
    native_phase_end(NP_ARRAY_NEW, result ? 1 : 0);
    return result;
}
static __attribute__((noinline)) void native_array_delete_phase(void *array) {
    uintptr_t target = native_phase_engine + 0x17e4b40;
    native_phase_begin(NP_ARRAY_DELETE, target);
    ((native_one_fn)target)(array);
    native_phase_end(NP_ARRAY_DELETE, 0);
}
static __attribute__((noinline)) void native_component_phase(void *component,
        uintptr_t unused1, uintptr_t unused2, uintptr_t unused3, uintptr_t target) {
    (void)unused1; (void)unused2; (void)unused3;
    native_phase_begin(NP_COMPONENT, target);
    ((native_one_fn)target)(component);
    native_phase_end(NP_COMPONENT, 0);
}

struct native_phase_site { uintptr_t offset; uint32_t word; uintptr_t wrapper; int indirect; };
static int native_branch_word(uintptr_t from, uintptr_t to, uint32_t link, uint32_t *word) {
    int64_t delta = (int64_t)to - (int64_t)from;
    if ((delta & 3) || delta < -INT64_C(134217728) || delta >= INT64_C(134217728)) return 0;
    *word = link | ((uint32_t)(delta / 4) & UINT32_C(0x03ffffff));
    return 1;
}
static void *native_phase_allocate_island(uintptr_t engine, const struct native_phase_site *sites,
        size_t count, size_t page_size, uint32_t *branches) {
    uintptr_t lowest = engine + sites[0].offset, highest = lowest;
    for (size_t i = 1; i < count; ++i) {
        uintptr_t pc = engine + sites[i].offset;
        if (pc < lowest) lowest = pc;
        if (pc > highest) highest = pc;
    }
    uintptr_t lower = highest > UINT32_C(134217728) ? highest - UINT32_C(134217728) : page_size;
    uintptr_t upper = lowest + UINT32_C(134217728) - 4;
    FILE *maps = fopen("/proc/self/maps", "r");
    if (!maps) return MAP_FAILED;
    void *island = MAP_FAILED;
    char line[1024];
    uintptr_t previous_end = lower;
    /* A maps snapshot supplies free-gap hints, never a reservation. Recheck the
     * actual returned mapping: a concurrent allocation may consume the gap. */
    while (fgets(line, sizeof(line), maps)) {
        unsigned long start, end;
        if (sscanf(line, "%lx-%lx", &start, &end) != 2) continue;
        if ((uintptr_t)end <= lower) continue;
        uintptr_t candidate = (previous_end + page_size - 1) & ~(page_size - 1);
        if (candidate <= upper && (uintptr_t)start >= candidate + page_size) {
            island = mmap((void *)candidate, page_size, PROT_READ | PROT_WRITE,
                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (island != MAP_FAILED) {
                size_t i = 0;
                for (; i < count; ++i) {
                    uint32_t link = sites[i].word >> 26 == 5 ? UINT32_C(0x14000000) : UINT32_C(0x94000000);
                    if (!native_branch_word(engine + sites[i].offset,
                            (uintptr_t)island + i * 32, link, &branches[i])) break;
                }
                if (i == count) break;
                munmap(island, page_size); island = MAP_FAILED;
            }
        }
        if ((uintptr_t)end > previous_end) previous_end = (uintptr_t)end;
        if (previous_end > upper) break;
    }
    fclose(maps);
    return island;
}
static int install_native_phases(uintptr_t engine) {
    if (native_phases_installed) return 1;
    const struct native_phase_site sites[] = {
        {0x1062144, 0x9400004c, (uintptr_t)native_state_phase, 0},
        {0x10623e8, 0x97f2345a, (uintptr_t)native_world_phase, 0},
        {0xcf04e4, 0xd63f0100, (uintptr_t)native_objects_phase, 1},
        {0x1062378, 0x97f33401, (uintptr_t)native_queue_phase, 0},
        {0x10625d8, 0x17f33369, (uintptr_t)native_queue_phase, 0},
        {0xd2fadc, 0xd63f0100, (uintptr_t)native_apply2_phase, 1},
        {0xd2f838, 0xd63f0100, (uintptr_t)native_apply3_phase, 1},
        {0xd2fdfc, 0xd63f0100, (uintptr_t)native_apply3_phase, 1},
        {0xf26458, 0x9422fa42, (uintptr_t)native_array_new_phase, 0},
        {0xf2676c, 0x1422f8f5, (uintptr_t)native_array_delete_phase, 0},
        {0xf26620, 0xd63f0100, (uintptr_t)native_component_phase, 1},
    };
    const size_t count = sizeof(sites) / sizeof(sites[0]);
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size < 4096 || (page_size & (page_size - 1))) return 0;
    for (size_t i = 0; i < count; ++i) {
        if (memcmp((void *)(engine + sites[i].offset), &sites[i].word, 4)) {
            printf("HEADLESS_NATIVE_PHASE_INSTALL_ERROR stage=guard offset=0x%lx\n", (unsigned long)sites[i].offset);
            fflush(stdout); return 0;
        }
    }
    uint32_t branches[sizeof(sites) / sizeof(sites[0])];
    void *island = native_phase_allocate_island(engine, sites, count, (size_t)page_size, branches);
    if (island == MAP_FAILED) {
        puts("HEADLESS_NATIVE_PHASE_INSTALL_ERROR stage=near-island");
        fflush(stdout); return 0;
    }
    for (size_t i = 0; i < count; ++i) {
        uint32_t stub[8] = {0};
        if (sites[i].indirect) {
            stub[0] = UINT32_C(0xaa0803e4); /* mov x4,x8: original virtual target */
            stub[1] = UINT32_C(0x58000070); /* ldr x16,literal +12 */
            stub[2] = UINT32_C(0xd61f0200); /* br x16 */
            stub[3] = UINT32_C(0xd503201f);
            memcpy(&stub[4], &sites[i].wrapper, 8);
        } else {
            stub[0] = UINT32_C(0x58000050); /* ldr x16,literal +8 */
            stub[1] = UINT32_C(0xd61f0200);
            memcpy(&stub[2], &sites[i].wrapper, 8);
        }
        memcpy((char *)island + i * 32, stub, sizeof(stub));
    }
    __builtin___clear_cache((char *)island, (char *)island + page_size);
    if (mprotect(island, (size_t)page_size, PROT_READ | PROT_EXEC)) return 0;
    uintptr_t pages[sizeof(sites) / sizeof(sites[0])]; size_t page_count = 0;
    for (size_t i = 0; i < count; ++i) {
        uintptr_t page = (engine + sites[i].offset) & ~((uintptr_t)page_size - 1);
        size_t j = 0; while (j < page_count && pages[j] != page) ++j;
        if (j == page_count) pages[page_count++] = page;
    }
    size_t writable = 0;
    for (; writable < page_count; ++writable) {
        if (mprotect((void *)pages[writable], (size_t)page_size, PROT_READ | PROT_WRITE)) {
            for (size_t j = 0; j < writable; ++j)
                mprotect((void *)pages[j], (size_t)page_size, PROT_READ | PROT_EXEC);
            return 0;
        }
    }
    for (size_t i = 0; i < count; ++i) memcpy((void *)(engine + sites[i].offset), &branches[i], 4);
    for (size_t i = 0; i < page_count; ++i) {
        __builtin___clear_cache((char *)pages[i], (char *)pages[i] + page_size);
        if (mprotect((void *)pages[i], (size_t)page_size, PROT_READ | PROT_EXEC)) return 0;
    }
    for (size_t i = 0; i < count; ++i)
        if (memcmp((void *)(engine + sites[i].offset), &branches[i], 4)) return 0;
    native_phase_engine = engine; native_phases_installed = 1;
    printf("HEADLESS_NATIVE_PHASE_INSTALL installed=1 sites=%zu scope=diagnostic-only\n", count);
    fflush(stdout);
    return 1;
}
#endif
