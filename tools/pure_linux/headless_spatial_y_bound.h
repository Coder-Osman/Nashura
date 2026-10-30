/* Experimental, opt-in optimization for the exact pinned ARM64 engine.
 *
 * Query +0xf34b3c iterates signed cells in x-major/y-minor order. At +0xf34c84,
 * y is already nonnegative; height <= y skips every remaining y in this x.
 * Their only operations are dimension reads and loop increments. The original
 * y end is an arithmetic shift of a signed 32-bit sum by 10, at most 2097151,
 * so incrementing through the end cannot wrap. Jump to the original outer-loop
 * increment, retaining the result buffer, ordering, filters and final cleanup.
 *
 * Scope assumes the engine's serial Mainloop owns this grid for the query.
 * No file bytes change. Installation occurs before the first battle task;
 * never toggle this in a running query. A changed build fails the byte guards.
 */
#ifndef NASHURA_HEADLESS_SPATIAL_Y_BOUND_H
#define NASHURA_HEADLESS_SPATIAL_Y_BOUND_H

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static int install_spatial_y_bound(uintptr_t engine) {
    static int installed;
    static const unsigned char entry[] = {
        0xff, 0x83, 0x02, 0xd1, 0xfd, 0x7b, 0x04, 0xa9,
        0xfc, 0x6f, 0x05, 0xa9, 0xfa, 0x67, 0x06, 0xa9};
    static const unsigned char outer[] = {
        0xe8, 0x0f, 0x40, 0xb9, 0x1f, 0x03, 0x08, 0x6b,
        0x18, 0x07, 0x00, 0x11, 0x80, 0x13, 0x00, 0x54};
    static const unsigned char original[] = {
        0x5b, 0xff, 0xff, 0x37, 0xe9, 0x06, 0x40, 0xb9,
        0x3f, 0x01, 0x1b, 0x6b, 0xed, 0xfe, 0xff, 0x54};
    const uint32_t replacement = UINT32_C(0x54fffdad); /* b.le +0xf34c38 */
    uintptr_t target = engine + 0xf34c84;
    if (installed) return memcmp((void *)target, &replacement, 4) == 0;
    if (memcmp((void *)(engine + 0xf34b3c), entry, sizeof(entry))
            || memcmp((void *)(engine + 0xf34c38), outer, sizeof(outer))
            || memcmp((void *)(engine + 0xf34c78), original, sizeof(original))) return 0;
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size < 4 || (page_size & (page_size - 1))) return 0;
    void *page = (void *)(target & ~((uintptr_t)page_size - 1));
    if (mprotect(page, (size_t)page_size, PROT_READ | PROT_WRITE)) return 0;
    memcpy((void *)target, &replacement, sizeof(replacement));
    __builtin___clear_cache((char *)target, (char *)target + sizeof(replacement));
    if (mprotect(page, (size_t)page_size, PROT_READ | PROT_EXEC)) return 0;
    installed = memcmp((void *)target, &replacement, sizeof(replacement)) == 0;
    printf("HEADLESS_NATIVE_OPTIMIZATION name=spatial-y-bound-v1 installed=%d offset=0xf34c84 target=0xf34c38\n",
           installed);
    fflush(stdout);
    return installed;
}

#endif
