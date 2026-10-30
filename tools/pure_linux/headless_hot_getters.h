/* Pinned-build experiments: replace eleven or fifty-two BL getter calls with their
 * original single LDR. No load is hoisted, cached, reordered or removed.
 *
 * Each original getter is exactly LDR {w,x}0,[x0,#field]; RET, changes no flags
 * or other registers, and has no side effects beyond the load. BL updates LR;
 * the audited callers save incoming LR before these sites and restore it before
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

/* Additional exact loads in sampled component-update callers. The candidate
 * includes the original eleven sites and is still disabled by default. */
static const struct hot_getter_site component_getter_sites[] = {
    {0xf6598c, 0xdc2600, UINT32_C(0x97f9731d), UINT32_C(0xf9400800)},
    {0xf65994, 0xdccc68, UINT32_C(0x97f99cb5), UINT32_C(0xf9400c00)},
    {0xf65a08, 0xf2d800, UINT32_C(0x97ff1f7e), UINT32_C(0xb9407c00)},
    {0xf65a14, 0xf2d808, UINT32_C(0x97ff1f7d), UINT32_C(0xb9408000)},
    {0xf65aa4, 0xcf0654, UINT32_C(0x97f62aec), UINT32_C(0xb9418800)},
    {0xf65ac8, 0xcf0654, UINT32_C(0x97f62ae3), UINT32_C(0xb9418800)},
    {0xf65b50, 0xcf0654, UINT32_C(0x97f62ac1), UINT32_C(0xb9418800)},
    {0xf65b80, 0x115d804, UINT32_C(0x9407df21), UINT32_C(0xf9402000)},
    {0xf65cf4, 0xf171b4, UINT32_C(0x97fec530), UINT32_C(0xf9402400)},
    {0xf65d44, 0xf171b4, UINT32_C(0x97fec51c), UINT32_C(0xf9402400)},
    {0xf65d48, 0xd98a68, UINT32_C(0x97f8cb48), UINT32_C(0xb9443800)},
    {0xf65d70, 0xf1b9e0, UINT32_C(0x97fed71c), UINT32_C(0xb9411c00)},
    {0xf65e24, 0xf1b9e0, UINT32_C(0x97fed6ef), UINT32_C(0xb9411c00)},
    {0xf65e38, 0xd98bd8, UINT32_C(0x97f8cb68), UINT32_C(0xb9444c00)},
    {0xf65ea8, 0xf1b9e0, UINT32_C(0x97fed6ce), UINT32_C(0xb9411c00)},
    {0xf65ec0, 0xd98bd8, UINT32_C(0x97f8cb46), UINT32_C(0xb9444c00)},
    {0xf65f9c, 0xd990f0, UINT32_C(0x97f8cc55), UINT32_C(0xb946a400)},
    {0xf65fbc, 0xf2d800, UINT32_C(0x97ff1e11), UINT32_C(0xb9407c00)},
    {0xf65fc8, 0xf2d808, UINT32_C(0x97ff1e10), UINT32_C(0xb9408000)},
    {0xf65ffc, 0xd990f0, UINT32_C(0x97f8cc3d), UINT32_C(0xb946a400)},
    {0xf66030, 0xd98bd8, UINT32_C(0x97f8caea), UINT32_C(0xb9444c00)},
    {0xf66054, 0xd98bd8, UINT32_C(0x97f8cae1), UINT32_C(0xb9444c00)},
    {0xf66060, 0xd990f0, UINT32_C(0x97f8cc24), UINT32_C(0xb946a400)},
    {0xf66070, 0xd990f0, UINT32_C(0x97f8cc20), UINT32_C(0xb946a400)},
    {0xf6607c, 0xd990f0, UINT32_C(0x97f8cc1d), UINT32_C(0xb946a400)},
    {0xf66094, 0xd990f0, UINT32_C(0x97f8cc17), UINT32_C(0xb946a400)},
    {0xf660a8, 0xd990f0, UINT32_C(0x97f8cc12), UINT32_C(0xb946a400)},
    {0xf66224, 0xd990f0, UINT32_C(0x97f8cbb3), UINT32_C(0xb946a400)},
    {0xf66320, 0xf2d820, UINT32_C(0x97ff1d40), UINT32_C(0xb9408c00)},
    {0xf6633c, 0xf2d820, UINT32_C(0x97ff1d39), UINT32_C(0xb9408c00)},
    {0xf663f0, 0xf171b4, UINT32_C(0x97fec371), UINT32_C(0xf9402400)},
    {0xf663f4, 0xd990c0, UINT32_C(0x97f8cb33), UINT32_C(0xb9432000)},
    {0xf66410, 0xd990c0, UINT32_C(0x97f8cb2c), UINT32_C(0xb9432000)},
    {0xf6641c, 0xd990c8, UINT32_C(0x97f8cb2b), UINT32_C(0xb9432400)},
    {0xf6653c, 0xcf0654, UINT32_C(0x97f62846), UINT32_C(0xb9418800)},
    {0xf66550, 0xcf33f0, UINT32_C(0x97f633a8), UINT32_C(0xf9406000)},
    {0xf66578, 0xcf0654, UINT32_C(0x97f62837), UINT32_C(0xb9418800)},
    {0xf66588, 0xcf33f0, UINT32_C(0x97f6339a), UINT32_C(0xf9406000)},
    {0xf665b0, 0xcf0654, UINT32_C(0x97f62829), UINT32_C(0xb9418800)},
    {0xf665ec, 0xf2d800, UINT32_C(0x97ff1c85), UINT32_C(0xb9407c00)},
    {0xf665f8, 0xf2d808, UINT32_C(0x97ff1c84), UINT32_C(0xb9408000)},
};
static const struct hot_getter_caller component_getter_callers[] = {
    {0xf6594c, {0xfd,0x7b,0xba,0xa9,0xfc,0x6f,0x01,0xa9,0xfa,0x67,0x02,0xa9,0xf8,0x5f,0x03,0xa9}},
    {0xf65cc8, {0xff,0x83,0x02,0xd1,0xfd,0x7b,0x04,0xa9,0xfc,0x6f,0x05,0xa9,0xfa,0x67,0x06,0xa9}},
};

static int install_hot_getter_variant(uintptr_t engine, int extended) {
    static int installed;
    const size_t original_count = sizeof(hot_getter_sites) / sizeof(hot_getter_sites[0]);
    const size_t extra_count = sizeof(component_getter_sites) / sizeof(component_getter_sites[0]);
    const size_t count = original_count + (extended ? extra_count : 0);
    struct hot_getter_site sites[original_count + extra_count];
    memcpy(sites, hot_getter_sites, sizeof(hot_getter_sites));
    if (extended) memcpy(sites + original_count, component_getter_sites, sizeof(component_getter_sites));
    if (installed && installed != (extended ? 2 : 1)) return 0;
    if (extended) {
        for (size_t i = 0; i < sizeof(component_getter_callers) / sizeof(component_getter_callers[0]); ++i)
            if (memcmp((void *)(engine + component_getter_callers[i].offset),
                       component_getter_callers[i].bytes, 16)) return 0;
    }
    const uint32_t ret = UINT32_C(0xd65f03c0);
    for (size_t i = 0; i < sizeof(hot_getter_callers) / sizeof(hot_getter_callers[0]); ++i)
        if (memcmp((void *)(engine + hot_getter_callers[i].offset),
                   hot_getter_callers[i].bytes, 16)) return 0;
    for (size_t i = 0; i < count; ++i) {
        const struct hot_getter_site *site = &sites[i];
        uint32_t expected = installed ? site->replacement : site->original;
        if (memcmp((void *)(engine + site->offset), &expected, 4)
                || memcmp((void *)(engine + site->getter), &site->replacement, 4)
                || memcmp((void *)(engine + site->getter + 4), &ret, 4)) return 0;
    }
    if (installed) return 1;
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size < 4 || (page_size & (page_size - 1))) return 0;
    uintptr_t pages[original_count + extra_count];
    size_t page_count = 0;
    for (size_t i = 0; i < count; ++i) {
        uintptr_t page = (engine + sites[i].offset) & ~((uintptr_t)page_size - 1);
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
        memcpy((void *)(engine + sites[i].offset), &sites[i].replacement, 4);
    for (size_t i = 0; i < page_count; ++i) {
        __builtin___clear_cache((char *)pages[i], (char *)pages[i] + page_size);
        if (mprotect((void *)pages[i], (size_t)page_size, PROT_READ | PROT_EXEC)) return 0;
    }
    installed = extended ? 2 : 1;
    for (size_t i = 0; i < count; ++i)
        if (memcmp((void *)(engine + sites[i].offset), &sites[i].replacement, 4)) return 0;
    printf("HEADLESS_NATIVE_OPTIMIZATION name=%s installed=1 sites=%zu\n",
        extended ? "component-getters-v1" : "hot-getters-v1", count);
    fflush(stdout);
    return 1;
}

static int install_hot_getters(uintptr_t engine) { return install_hot_getter_variant(engine, 0); }
static int install_component_getters(uintptr_t engine) { return install_hot_getter_variant(engine, 1); }

#endif
