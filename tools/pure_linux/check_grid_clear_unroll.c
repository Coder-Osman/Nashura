/* ARM64 instruction parity; synthetic grids do not prove full-match parity. */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdlib.h>
#include "headless_grid_clear_unroll.h"
typedef void (*clear_fn)(void *);
static uint64_t rng = UINT64_C(0x61d20261031);
static uint32_t random32(void) {
    rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
    return (uint32_t)rng;
}
static void put32(void *p, uint32_t v) { memcpy(p, &v, 4); }
static int check(clear_fn original, clear_fn candidate, uint32_t w, uint32_t h,
                 unsigned char *table_end, unsigned char *cell_end, size_t maximum) {
    int32_t count = (int32_t)(w*h);
    size_t n = count > 0 ? (size_t)count : 0;
    if (n > maximum) return 0;
    unsigned char grid[80], cells_before[64], grid_after[80];
    size_t cell_bytes = maximum * 32;
    unsigned char *cells = cell_end - cell_bytes;
    uintptr_t *table = (uintptr_t *)(void *)(table_end - n*8);
    memset(cells, 0xa5, cell_bytes);
    for (size_t i = 0; i < n; ++i)
        table[i] = (uintptr_t)(cells + (random32() % maximum)*32 + 16);
    /* Last count occupies the last four readable bytes: no over-wide store. */
    if (n) table[n-1] = (uintptr_t)(cell_end - 16);
    memset(grid, 0x5a, sizeof(grid)); put32(grid, w); put32(grid+4, h);
    uintptr_t p = n ? (uintptr_t)table : 0; memcpy(grid+16, &p, 8);
    memcpy(cells_before, grid, sizeof(cells_before));
    unsigned char *expected = malloc(cell_bytes);
    if (!expected) return 0;
    original(grid); memcpy(grid_after, grid, sizeof(grid)); memcpy(expected, cells, cell_bytes);
    memcpy(grid, cells_before, sizeof(cells_before)); memset(grid+64, 0x5a, 16);
    memset(cells, 0xa5, cell_bytes); candidate(grid);
    int equal = !memcmp(grid, grid_after, sizeof(grid)) && !memcmp(cells, expected, cell_bytes);
    free(expected); return equal;
}
int main(void) {
    long page = sysconf(_SC_PAGESIZE);
    const size_t maximum = 4099;
    size_t table_bytes = (maximum*8 + page-1) & ~((size_t)page-1);
    size_t cell_bytes = (maximum*32 + page-1) & ~((size_t)page-1);
    size_t span = (0xf35000 + page-1) & ~((size_t)page-1);
    unsigned char *engine = mmap(NULL,span,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *copy = mmap(NULL,page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *table = mmap(NULL,table_bytes+page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *cells = mmap(NULL,cell_bytes+page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(engine==MAP_FAILED||copy==MAP_FAILED||table==MAP_FAILED||cells==MAP_FAILED)return 2;
    if(mprotect(table+table_bytes,page,PROT_NONE)||mprotect(cells+cell_bytes,page,PROT_NONE))return 3;
    memcpy(copy,grid_clear_original,sizeof(grid_clear_original));
    memcpy(engine+0xf34864,grid_clear_original,sizeof(grid_clear_original));
    __builtin___clear_cache((char *)copy,(char *)copy+page);
    if(mprotect(copy,page,PROT_READ|PROT_EXEC))return 4;
    engine[0xf34864]^=1;
    if(install_grid_clear_unroll((uintptr_t)engine))return 5;
    engine[0xf34864]^=1;
    if(!install_grid_clear_unroll((uintptr_t)engine)||!install_grid_clear_unroll((uintptr_t)engine))return 6;
    clear_fn a=(clear_fn)(void *)copy,b=(clear_fn)(void *)(engine+0xf34864);
    uint64_t cases=0;
    for(uint32_t n=0;n<=maximum;++n) {
        if(!check(a,b,n,1,table+table_bytes,cells+cell_bytes,maximum))return 7;
        ++cases;
    }
    const uint32_t dimensions[][2]={{0,0},{0xffffffff,1},{0x80000000,1},
        {0xffffffff,0xffffffff},{0x80000001,2},{65536,65536},{0xffff0000,65536}};
    for(size_t i=0;i<sizeof(dimensions)/sizeof(dimensions[0]);++i){
        if(!check(a,b,dimensions[i][0],dimensions[i][1],table+table_bytes,cells+cell_bytes,maximum))return 8;
        ++cases;
    }
    for(unsigned i=0;i<20000;++i){
        uint32_t w=random32()%64,h=random32()%64;
        if(!check(a,b,w,h,table+table_bytes,cells+cell_bytes,maximum))return 9;
        ++cases;
    }
    printf("GRID_CLEAR_UNROLL_CHECK cases=%" PRIu64 " original_and_candidate=equal guard_pages=passed duplicate_cells=covered signed_modular_product=covered\n",cases);
    return 0;
}
