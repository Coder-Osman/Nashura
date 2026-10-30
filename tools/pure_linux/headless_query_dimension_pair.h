/* Fixed-build query-loop experiment. Pair-load adjacent dimensions once per
 * visited iteration; no dimension cache across calls/iterations. All filters,
 * result ordering, cell/entry reads and cleanup remain in the original body.
 * Scope: valid complete grid object, serial Mainloop dimensions stable during
 * each iteration. Height is read before width/y rejection; fault/observation
 * access timing differs. Not for MMIO, volatile dimensions or concurrent writes.
 */
#ifndef NASHURA_HEADLESS_QUERY_DIMENSION_PAIR_H
#define NASHURA_HEADLESS_QUERY_DIMENSION_PAIR_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static const uint32_t query_dimension_original[] = {
    0xb94002e8,0x6b18011f,0x54ffff6d,0x37ffff5b,0xb94006e9,0x6b1b013f,
    0x54fffeed,0x1b1b6108,0xf9400ae9,0xf868d939,0xb4fffe79,0xb9400f28,
    0x7100051f,0x54fffe0b,0xd37df116,0xb94013e8,0x34000808,0xaa1f03f3,
    0x1400000a
};
static const uint32_t query_dimension_replacement[] = {
    0x294026e8,0x6b18011f,0x54ffff6d,0x37ffff5b,0x6b1b013f,0x54ffff0d,
    0x1b1b6108,0xf9400ae9,0xf868d939,0xb4fffe99,0xb9400f28,0x7100051f,
    0x54fffe2b,0xd37df116,0xb94013e8,0x34000828,0xaa1f03f3,0x1400000b,
    0xd503201f
};
static int install_query_dimension_pair(uintptr_t engine) {
    static int installed;
    void *target = (void *)(engine + 0xf34c6c);
    const size_t bytes = sizeof(query_dimension_original);
    /* Sole external immediate branch enters the unchanged start address. */
    const uint32_t entry_branch = UINT32_C(0x540000ac);
    if (memcmp((void *)(engine + 0xf34c58), &entry_branch, 4)) return 0;
    if (installed) return memcmp(target, query_dimension_replacement, bytes) == 0;
    if (memcmp(target, query_dimension_original, bytes)) return 0;
    long size = sysconf(_SC_PAGESIZE);
    if (size < 4 || (size & (size - 1))) return 0;
    uintptr_t page = (uintptr_t)target & ~((uintptr_t)size - 1);
    if ((((uintptr_t)target + bytes - 1) & ~((uintptr_t)size - 1)) != page) return 0;
    if (mprotect((void *)page, (size_t)size, PROT_READ | PROT_WRITE)) return 0;
    memcpy(target, query_dimension_replacement, bytes);
    __builtin___clear_cache((char *)target, (char *)target + bytes);
    if (mprotect((void *)page, (size_t)size, PROT_READ | PROT_EXEC)) return 0;
    installed = memcmp(target, query_dimension_replacement, bytes) == 0;
    printf("HEADLESS_NATIVE_OPTIMIZATION name=query-dimension-pair-v1 installed=%d offset=0xf34c6c bytes=76\n", installed);
    fflush(stdout);
    return installed;
}
#endif
