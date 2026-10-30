/* Pinned ARM64 grid-clear experiment, disabled by default.
 * Preserve signed 32-bit width*height, flag clear, pairwise pointer reads and
 * count stores in their original order. Unroll two pairs per loop; no cache,
 * skipped store, hoisted pointer read, SIMD access or Android dependency.
 * Only AAPCS64 caller-saved registers and NZCV differ. Normal serial Mainloop
 * semantics are covered; fault PCs and profiler instruction addresses differ.
 */
#ifndef NASHURA_HEADLESS_GRID_CLEAR_UNROLL_H
#define NASHURA_HEADLESS_GRID_CLEAR_UNROLL_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static const uint32_t grid_clear_original[] = {
    0x29402408,0x3901001f,0x1b087d28,0x7100051f,0x540002eb,0xf9400809,
    0x7100051f,0x54000061,0xaa1f03ea,0x1400000c,0x927f750a,0x9100212b,
    0xaa0a03ec,0xa97fb96d,0xf100098c,0x9100416b,0xb9000dbf,0xb9000ddf,
    0x54ffff61,0xeb08015f,0x540000e0,0x8b0a0d29,0xcb0a0108,0xf840852a,
    0xf1000508,0xb9000d5f,0x54ffffa1,0xd65f03c0
};
static const uint32_t grid_clear_replacement[] = {
    0x29402408,0x3901001f,0x1b087d28,0x7100051f,0x540002cb,0xf9400809,
    0x7100111f,0x5400014b,0xa8c12d2a,0xb9000d5f,0xb9000d7f,0xa8c12d2a,
    0xb9000d5f,0xb9000d7f,0x51001108,0x7100111f,0x54ffff0a,0x7100091f,
    0x540000ab,0xa8c12d2a,0xb9000d5f,0xb9000d7f,0x51000908,0x34000068,
    0xf940012a,0xb9000d5f,0xd65f03c0,0xd503201f
};

static int install_grid_clear_unroll(uintptr_t engine) {
    static int installed;
    void *target = (void *)(engine + 0xf34864);
    const size_t bytes = sizeof(grid_clear_original);
    _Static_assert(sizeof(grid_clear_replacement) == sizeof(grid_clear_original), "fixed span");
    if (installed) return memcmp(target, grid_clear_replacement, bytes) == 0;
    if (memcmp(target, grid_clear_original, bytes)) return 0;
    long size = sysconf(_SC_PAGESIZE);
    if (size < 4 || (size & (size - 1))) return 0;
    uintptr_t page = (uintptr_t)target & ~((uintptr_t)size - 1);
    if ((((uintptr_t)target + bytes - 1) & ~((uintptr_t)size - 1)) != page) return 0;
    if (mprotect((void *)page, (size_t)size, PROT_READ | PROT_WRITE)) return 0;
    memcpy(target, grid_clear_replacement, bytes);
    __builtin___clear_cache((char *)target, (char *)target + bytes);
    if (mprotect((void *)page, (size_t)size, PROT_READ | PROT_EXEC)) return 0;
    installed = memcmp(target, grid_clear_replacement, bytes) == 0;
    printf("HEADLESS_NATIVE_OPTIMIZATION name=grid-clear-unroll-v1 installed=%d offset=0xf34864 bytes=112\n", installed);
    fflush(stdout);
    return installed;
}
#endif
