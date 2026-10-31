/* Native block parity with original displacements and guarded memory edges.
 * This compares relevant live outputs, not fault timing or dead w9 on reject.
 */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdlib.h>
#ifdef QUERY_SIGNED_HEIGHT_CHECK
#include "headless_query_signed_height.h"
#define install_candidate install_query_signed_height
#define CHECK_LABEL "QUERY_SIGNED_HEIGHT_CHECK"
#else
#include "headless_query_dimension_pair.h"
#define install_candidate install_query_dimension_pair
#define CHECK_LABEL "QUERY_DIMENSION_PAIR_CHECK"
#endif
extern void query_dimension_invoke(void *, void *, uint32_t, uint32_t, uint32_t, uint64_t *);
static uint64_t rng = UINT64_C(0x71d20261031);
static uint32_t random32(void) {
    rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
    return (uint32_t)rng;
}
static void put32(void *p, uint32_t v) { memcpy(p, &v, 4); }
static void putptr(void *p, void *v) { memcpy(p, &v, sizeof(v)); }
static void stubs(unsigned char *base) {
#ifdef QUERY_SIGNED_HEIGHT_CHECK
    const uintptr_t offsets[] = {0xf34c38, 0xf34dac, 0xf34cdc};
    const uint32_t reject[]={0x6b1c037f,0x1100077b,0x54fffe80};
    memcpy(base+0xf34c60,reject,sizeof(reject));
#else
    const uintptr_t offsets[] = {0xf34c60, 0xf34dac, 0xf34cdc};
#endif
    for (unsigned i = 0; i < 3; ++i) {
        uint32_t code[] = {UINT32_C(0x52800000) | (i << 5), UINT32_C(0xd65f03c0)};
        memcpy(base + offsets[i], code, sizeof(code));
    }
    const uint32_t entry = UINT32_C(0x540000ac);
    memcpy(base + 0xf34c58, &entry, 4);
    memcpy(base + 0xf34c6c, query_dimension_original, sizeof(query_dimension_original));
}
static int check(unsigned char *old, unsigned char *candidate, unsigned char *grid,
                 unsigned char *table_end, unsigned char *cell, uint32_t width,
                 uint32_t height, uint32_t x, uint32_t y, uint32_t count,
                 uint32_t filter, int null_cell) {
    uint64_t a[7] = {0}, b[7] = {0};
    unsigned char saved_grid[64], saved_cell[16];
    memset(grid, 0x5a, 64); memset(cell, 0xa5, 16);
    put32(grid, width); put32(grid + 4, height); put32(cell + 12, count);
    int reject = (int32_t)width <= (int32_t)x || (int32_t)y < 0 || (int32_t)height <= (int32_t)y;
    uint32_t index = width * y + x;
    unsigned char *table = NULL;
    if (!reject) {
        if (index >= 4096) return 0;
        table = table_end - ((size_t)index + 1) * 8;
        putptr(table + (size_t)index * 8, null_cell ? NULL : cell);
    }
    putptr(grid + 16, table);
    memcpy(saved_grid, grid, sizeof(saved_grid)); memcpy(saved_cell, cell, sizeof(saved_cell));
    query_dimension_invoke(old + 0xf34c6c, grid, x, y, filter, a);
    query_dimension_invoke(candidate + 0xf34c6c, grid, x, y, filter, b);
    // w9 is dead on width/y rejection; early height load intentionally differs.
    if ((int32_t)width <= (int32_t)x || (int32_t)y < 0) a[1] = b[1] = 0;
#ifdef QUERY_SIGNED_HEIGHT_CHECK
    /* In the real reject target +0xf34c60, CMP replaces NZCV and w9 is dead. */
    if (reject) { a[1]=b[1]=0; a[5]=b[5]=0; }
#endif
    uint64_t expected = reject || null_cell || (int32_t)count < 1 ? 0 : filter ? 2 : 1;
    return !memcmp(a, b, sizeof(a)) && a[6] == expected
        && !memcmp(grid, saved_grid, sizeof(saved_grid))
        && !memcmp(cell, saved_cell, sizeof(saved_cell));
}
int main(void) {
    (void)install_query_dimension_pair;
    long page = sysconf(_SC_PAGESIZE);
    if (page < 64 || (page & (page - 1))) return 2;
    size_t span = (0xf35000 + page - 1) & ~((size_t)page - 1);
    size_t table_bytes = (4096 * 8 + page - 1) & ~((size_t)page - 1);
    unsigned char *old = mmap(NULL,span,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *candidate = mmap(NULL,span,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *grid = mmap(NULL,page*2,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *cell = mmap(NULL,page*2,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *table = mmap(NULL,table_bytes+page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if (old==MAP_FAILED || candidate==MAP_FAILED || grid==MAP_FAILED || cell==MAP_FAILED || table==MAP_FAILED) return 3;
    if (mprotect(grid+page,page,PROT_NONE) || mprotect(cell+page,page,PROT_NONE)
            || mprotect(table+table_bytes,page,PROT_NONE)) return 4;
    stubs(old); stubs(candidate);
    candidate[0xf34c58] ^= 1;
    if (install_candidate((uintptr_t)candidate)) return 5;
    candidate[0xf34c58] ^= 1; candidate[0xf34c6c] ^= 1;
    if (install_candidate((uintptr_t)candidate)) return 6;
    candidate[0xf34c6c] ^= 1;
    __builtin___clear_cache((char *)old + 0xf34000, (char *)old + 0xf35000);
    __builtin___clear_cache((char *)candidate + 0xf34000, (char *)candidate + 0xf35000);
    uintptr_t code_page = ((uintptr_t)old + 0xf34c6c) & ~((uintptr_t)page-1);
    if (mprotect((void *)code_page,page,PROT_READ|PROT_EXEC)) return 7;
    if (!install_candidate((uintptr_t)candidate)
            || !install_candidate((uintptr_t)candidate)) return 8;
    uint64_t cases = 0;
    const uint32_t edges[] = {0,1,2,31,32,0x7fffffff,0x80000000,0xffffffff};
    const uint32_t counts[] = {0,1,2,0x7fffffff,0x80000000,0xffffffff};
    for (unsigned w=0; w<8; ++w) for (unsigned h=0; h<8; ++h)
        for (unsigned x=0; x<8; ++x) for (unsigned y=0; y<8; ++y) {
            int reject = (int32_t)edges[w] <= (int32_t)edges[x] || (int32_t)edges[y] < 0
                || (int32_t)edges[h] <= (int32_t)edges[y];
            if (!reject && (uint64_t)edges[w]*edges[y]+edges[x]>=4096) continue;
            for (unsigned c=0; c<6; ++c) for (unsigned f=0; f<2; ++f)
                for (unsigned n=0; n<2; ++n) {
                    if (!check(old,candidate,grid+page-64,table+table_bytes,cell+page-16,
                            edges[w],edges[h],edges[x],edges[y],counts[c],f,n)) return 9;
                    ++cases;
                }
        }
    for (unsigned i=0; i<100000; ++i) {
        uint32_t w=random32()%64,h=random32()%64,x=random32()%65,y=random32()%65;
        if (!check(old,candidate,grid+page-64,table+table_bytes,cell+page-16,
                w,h,x,y,counts[random32()%6],random32()&1,random32()&1)) return 10;
        ++cases;
    }
    printf(CHECK_LABEL " cases=%" PRIu64
        " original_and_candidate=equal guard_pages=passed signed_rejection=covered null_cell=covered count_edges=covered exits=3\n",cases);
    return 0;
}
