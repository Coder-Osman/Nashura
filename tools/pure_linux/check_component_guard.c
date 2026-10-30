/* Execute the exact original body and installed branch island on real ARM64.
 * Synthetic ordinary-memory objects exercise semantic and ABI boundaries;
 * complete-engine all-frame validation remains a separate gate. */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdlib.h>
#include "headless_native_phases.h"
#include "headless_component_guard.h"
extern void component_guard_invoke(void *,void *,int32_t,int32_t,uint32_t,void *,uint64_t *);
static uint64_t type_calls, obstacle_calls;
static int type_value, obstacle_value;
static int side_effect_type(void *p) { (void)p; ++type_calls; return type_value; }
static int obstacle(void *p,int32_t x,int32_t y) {
    (void)p; (void)x; (void)y; ++obstacle_calls; return obstacle_value;
}
static void put32(void *p,uint32_t v) { memcpy(p,&v,4); }
static void putptr(void *p,void *v) { memcpy(p,&v,sizeof(v)); }
static void prepare(unsigned char *base) {
    memcpy(base+0x115d540,component_guard_original,sizeof(component_guard_original));
    const struct {uintptr_t off;uint32_t words[2];} getters[]={
        {0xf20210,{0x528000a0,0xd65f03c0}},{0xf1b9e0,{0xb9411c00,0xd65f03c0}},
        {0xf171b4,{0xf9402400,0xd65f03c0}},{0xd98ca4,{0xf9413000,0xd65f03c0}}
    };
    for(size_t i=0;i<sizeof(getters)/sizeof(getters[0]);++i)
        memcpy(base+getters[i].off,getters[i].words,8);
    const uint32_t veneer[]={0x58000050,0xd61f0200};
    memcpy(base+0xe379d8,veneer,8);
    uintptr_t target=(uintptr_t)obstacle;memcpy(base+0xe379e0,&target,8);
}
static uint64_t rng=UINT64_C(0x20261031cafe);
static uint32_t random32(void) { rng^=rng<<13;rng^=rng>>7;rng^=rng<<17;return(uint32_t)rng; }
int main(void) {
    long page=sysconf(_SC_PAGESIZE);if(page<4096 || (page&(page-1)))return 2;
    size_t span=(0x115e000+page-1)&~((size_t)page-1);
    unsigned char *old=mmap(NULL,span,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *candidate=mmap(NULL,span,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *memory=mmap(NULL,page*6,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(old==MAP_FAILED || candidate==MAP_FAILED || memory==MAP_FAILED)return 3;
    if(mprotect(memory+page,page,PROT_NONE)||mprotect(memory+3*page,page,PROT_NONE)
        ||mprotect(memory+5*page,page,PROT_NONE))return 4;
    unsigned char *grid=memory+page-80,*object=memory+3*page-0x120,*nested=memory+5*page-0x268;
    uintptr_t table[3]={0}; unsigned char saved_grid[80],saved_object[0x120],saved_nested[0x268];
    prepare(old);prepare(candidate);
    candidate[0xf20210]^=1;if(install_component_guard((uintptr_t)candidate,0))return 5;
    candidate[0xf20210]^=1;candidate[0x115d5a0]^=1;
    if(install_component_guard((uintptr_t)candidate,0))return 6;candidate[0x115d5a0]^=1;
    __builtin___clear_cache((char *)old,(char *)old+span);
    __builtin___clear_cache((char *)candidate,(char *)candidate+span);
    if(mprotect(old,span,PROT_READ|PROT_EXEC)||mprotect(candidate,span,PROT_READ|PROT_EXEC))return 7;
    if(!install_component_guard((uintptr_t)candidate,0)||!install_component_guard((uintptr_t)candidate,0))return 8;
    uint64_t cases=0,matched=0,fallback=0,nulls=0,rejected=0;
    const uint32_t edges[]={0,1,2,31,0x7fffffff,0x80000000,0xffffffff};
    for(unsigned i=0;i<120000;++i) {
        uint32_t w=i<2401?edges[(i/343)%7]:random32()%64;
        uint32_t h=i<2401?edges[(i/49)%7]:random32()%64;
        int32_t x=(int32_t)(i<2401?edges[(i/7)%7]:random32()%65);
        int32_t y=(int32_t)(i<2401?edges[i%7]:random32()%65);
        uint32_t flag=(i%4==0)?0:(i%4==1)?2:(i%4==2)?1:0xffffffff;
        int mode=i%3;type_value=(i%5==0)?5:7;obstacle_value=i%2;
        memset(grid,0x5a,80);memset(object,0xa5,0x120);memset(nested,0x3c,0x268);
        put32(grid,w);put32(grid+4,h);putptr(grid+72,nested);
        putptr(object,table);put32(object+0x11c,i%2?6:9);putptr(object+72,nested);
        putptr(nested+0x260,i%7?NULL:object);
        memcpy(saved_grid,grid,80);memcpy(saved_object,object,0x120);memcpy(saved_nested,nested,0x268);
        int reject=x<0||y<0||(int32_t)w<=x||(int32_t)h<=y;
        void *g=reject&&(x<0||y<0)?NULL:grid;void *o=mode==0?NULL:object;
        uint64_t a[12]={0},b[12]={0},tc[2],oc[2];
        for(int side=0;side<2;++side) {
            unsigned char *base=side?candidate:old;
            table[2]=mode==1?(uintptr_t)base+0xf20210:(uintptr_t)side_effect_type;
            type_calls=0;obstacle_calls=0;
            component_guard_invoke(base+0x115d540,g,x,y,flag,o,side?b:a);
            tc[side]=type_calls;oc[side]=obstacle_calls;
        }
        if(memcmp(a,b,sizeof(a))||tc[0]!=tc[1]||oc[0]!=oc[1]
            ||memcmp(grid,saved_grid,80)||memcmp(object,saved_object,0x120)||memcmp(nested,saved_nested,0x268)) {
            fprintf(stderr,"parity failure case=%u return=%llu/%llu type=%llu/%llu obstacle=%llu/%llu\n",i,
                (unsigned long long)a[0],(unsigned long long)b[0],(unsigned long long)tc[0],
                (unsigned long long)tc[1],(unsigned long long)oc[0],(unsigned long long)oc[1]);return 9;
        }
        for(unsigned j=1;j<12;++j)if(a[j]!=0x118+j)return 10;
        ++cases;if(reject)++rejected;else if(flag&1||mode==2)++fallback;else if(mode==0)++nulls;else++matched;
    }
    if(!matched||!fallback||!nulls||!rejected)return 11;
    printf("COMPONENT_GUARD_CHECK cases=%"PRIu64" original_and_candidate=equal guard_pages=passed callee_saved=passed side_effect_fallback=passed matched=%"PRIu64" fallback=%"PRIu64" null=%"PRIu64" rejected=%"PRIu64"\n",cases,matched,fallback,nulls,rejected);
    return 0;
}
