/* Pinned-build experiment: replace eleven hot BL getter calls with their
 * original single LDR. No load is hoisted, cached, reordered or removed.
 *
 * Each original getter is exactly LDR {w,x}0,[x0,#field]; RET, changes no flags
 * or other registers, and has no side effects beyond the load. BL updates LR;
 * all three callers save incoming LR before these sites and restore it before
 * returning, with no other LR reads in the intervening body. This keeps normal
 * execution semantics, not signal/profiler call stacks or fault PCs.
 *
 * Install before the first battle on Mainloop. Original file bytes stay fixed.
 * This experiment is independent of spatial-y-bound-v1; never combine them.
 */
#ifndef NASHURA_HEADLESS_HOT_GETTERS_H
#define NASHURA_HEADLESS_HOT_GETTERS_H

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static const struct hot_getter_site {
    uintptr_t offset, getter;
    uint32_t original, replacement;
} hot_getter_sites[] = {
    {0xf6703c, 0xcf0654, UINT32_C(0x97f62586), UINT32_C(0xb9418800)},
    {0xf67190, 0xcf0654, UINT32_C(0x97f62531), UINT32_C(0xb9418800)},
    {0x115d5b0, 0xf1b9e0, UINT32_C(0x97f6f90c), UINT32_C(0xb9411c00)},
    {0x115d5c0, 0xf171b4, UINT32_C(0x97f6e6fd), UINT32_C(0xf9402400)},
    {0x115d5c4, 0xd98ca4, UINT32_C(0x97f0edb8), UINT32_C(0xf9413000)},
    {0xf5d814, 0xf171b4, UINT32_C(0x97fee668), UINT32_C(0xf9402400)},
    {0xf5d894, 0xf1b9e0, UINT32_C(0x97fef853), UINT32_C(0xb9411c00)},
    {0xf5d8c4, 0xf171b4, UINT32_C(0x97fee63c), UINT32_C(0xf9402400)},
    {0xf5d8d4, 0xf171b4, UINT32_C(0x97fee638), UINT32_C(0xf9402400)},
    {0xf5db30, 0xf171b4, UINT32_C(0x97fee5a1), UINT32_C(0xf9402400)},
    {0xf5db44, 0xf171b4, UINT32_C(0x97fee59c), UINT32_C(0xf9402400)},
};

static const struct hot_getter_caller { uintptr_t offset; unsigned char bytes[16]; }
hot_getter_callers[] = {
    {0xf66e30, {0xff,0xc3,0x02,0xd1,0xfd,0x7b,0x05,0xa9,0xfc,0x6f,0x06,0xa9,0xfa,0x67,0x07,0xa9}},
    {0x115d540,{0xe8,0x03,0x00,0xaa,0x49,0x00,0x01,0x2a,0xe0,0x03,0x1f,0x2a,0xa9,0x05,0xf8,0x37}},
    {0xf5d7e0, {0xff,0x03,0x03,0xd1,0xfd,0x7b,0x06,0xa9,0xfc,0x6f,0x07,0xa9,0xfa,0x67,0x08,0xa9}},
};

static int install_hot_getters(uintptr_t engine) {
    static int installed;
    const size_t count = sizeof(hot_getter_sites) / sizeof(hot_getter_sites[0]);
    const uint32_t ret = UINT32_C(0xd65f03c0);
    for (size_t i = 0; i < sizeof(hot_getter_callers) / sizeof(hot_getter_callers[0]); ++i)
        if (memcmp((void *)(engine + hot_getter_callers[i].offset),
                   hot_getter_callers[i].bytes, 16)) return 0;
    for (size_t i = 0; i < count; ++i) {
        const struct hot_getter_site *site = &hot_getter_sites[i];
        uint32_t expected = installed ? site->replacement : site->original;
        if (memcmp((void *)(engine + site->offset), &expected, 4)
                || memcmp((void *)(engine + site->getter), &site->replacement, 4)
                || memcmp((void *)(engine + site->getter + 4), &ret, 4)) return 0;
    }
    if (installed) return 1;
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size < 4 || (page_size & (page_size - 1))) return 0;
    uintptr_t pages[sizeof(hot_getter_sites) / sizeof(hot_getter_sites[0])];
    size_t page_count = 0;
    for (size_t i = 0; i < count; ++i) {
        uintptr_t page = (engine + hot_getter_sites[i].offset) & ~((uintptr_t)page_size - 1);
        size_t j = 0;
        while (j < page_count && pages[j] != page) ++j;
        if (j == page_count) pages[page_count++] = page;
    }
    /* Verify everything before mutation; never leave a page writable/executable.
     * On permission failure caller aborts the disposable process, not a retry. */
    size_t writable = 0;
    for (; writable < page_count; ++writable) {
        if (mprotect((void *)pages[writable], (size_t)page_size, PROT_READ | PROT_WRITE)) {
            for (size_t j = 0; j < writable; ++j)
                mprotect((void *)pages[j], (size_t)page_size, PROT_READ | PROT_EXEC);
            return 0;
        }
    }
    for (size_t i = 0; i < count; ++i)
        memcpy((void *)(engine + hot_getter_sites[i].offset), &hot_getter_sites[i].replacement, 4);
    for (size_t i = 0; i < page_count; ++i) {
        __builtin___clear_cache((char *)pages[i], (char *)pages[i] + page_size);
        if (mprotect((void *)pages[i], (size_t)page_size, PROT_READ | PROT_EXEC)) return 0;
    }
    installed = 1;
    for (size_t i = 0; i < count; ++i)
        if (memcmp((void *)(engine + hot_getter_sites[i].offset), &hot_getter_sites[i].replacement, 4)) return 0;
    printf("HEADLESS_NATIVE_OPTIMIZATION name=hot-getters-v1 installed=1 sites=%zu\n", count);
    fflush(stdout);
    return 1;
}

#endif
