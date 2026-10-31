/* Pinned-build, opt-in query block. Signed 32-bit height is sign-extended to
 * 64 bits and compared against zero-extended y. Signed height > unsigned y
 * is exactly y>=0 && signed height>signed y for every 32-bit height/y.
 * This removes one conditional branch without caching dimensions. Width,
 * cell/entry reads, filters and output cleanup remain in the original order.
 * Height is now read on negative y: valid complete ordinary-memory grids
 * only; MMIO/volatile/fault timing and asynchronous stacks are excluded.
 * w9 and NZCV differ only on reject, where the original next block overwrites
 * them before use. No dimension stability beyond each local block is added.
 */
#ifndef NASHURA_HEADLESS_QUERY_SIGNED_HEIGHT_H
#define NASHURA_HEADLESS_QUERY_SIGNED_HEIGHT_H
#include "headless_query_dimension_pair.h"

static const uint32_t query_signed_height_replacement[] = {
    0xb94002e8,0x6b18011f,0x54ffff6d,0xb98006e9,0xeb3b413f,0x54ffff0d,
    0x1b1b6108,0xf9400ae9,0xf868d939,0xb4fffe99,0xb9400f28,0x7100051f,
    0x54fffe2b,0xd37df116,0xb94013e8,0x34000828,0xaa1f03f3,0x1400000b,
    0xd503201f
};
static int install_query_signed_height(uintptr_t engine) {
    static uintptr_t installed_engine;
    void *target=(void *)(engine+0xf34c6c);
    const size_t bytes=sizeof(query_dimension_original);
    const uint32_t entry_branch=UINT32_C(0x540000ac);
    const uint32_t reject[]={0x6b1c037f,0x1100077b,0x54fffe80};
    if (memcmp((void *)(engine+0xf34c58),&entry_branch,4)
            || memcmp((void *)(engine+0xf34c60),reject,sizeof(reject))) return 0;
    if (installed_engine) return installed_engine==engine
        && memcmp(target,query_signed_height_replacement,bytes)==0;
    if (memcmp(target,query_dimension_original,bytes)) return 0;
    long size=sysconf(_SC_PAGESIZE);
    if (size<4 || (size&(size-1))) return 0;
    uintptr_t page=(uintptr_t)target&~((uintptr_t)size-1);
    if ((((uintptr_t)target+bytes-1)&~((uintptr_t)size-1))!=page) return 0;
    if (mprotect((void *)page,(size_t)size,PROT_READ|PROT_WRITE)) return 0;
    memcpy(target,query_signed_height_replacement,bytes);
    __builtin___clear_cache((char *)target,(char *)target+bytes);
    if (mprotect((void *)page,(size_t)size,PROT_READ|PROT_EXEC)) return 0;
    if (memcmp(target,query_signed_height_replacement,bytes)) return 0;
    installed_engine=engine;
    printf("HEADLESS_NATIVE_OPTIMIZATION name=query-signed-height-v1 installed=1 offset=0xf34c6c bytes=76\n");
    fflush(stdout);
    return 1;
}
#endif
