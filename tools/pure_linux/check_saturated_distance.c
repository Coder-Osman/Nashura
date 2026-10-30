/* Run on ARM64 Linux/Android: execute exact pinned instructions and candidate. */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdlib.h>
#include "headless_saturated_distance.h"

typedef uint32_t (*pointer_distance)(const uint32_t *,uint32_t,uint32_t);
typedef uint32_t (*scalar_distance)(uint32_t,uint32_t,uint32_t,uint32_t);
static uint64_t rng = UINT64_C(0xd157aace20261031);
static uint64_t cases;
static uint32_t next_random(void) {
    rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17;
    return (uint32_t)rng;
}
static uint32_t reference(uint32_t a,uint32_t b,uint32_t c,uint32_t d) {
    int64_t x = (int32_t)(a-c), y = (int32_t)(b-d);
    uint64_t sum = (uint64_t)(x*x) + (uint64_t)(y*y);
    return sum < UINT32_C(0x7fffffff) ? (uint32_t)sum : UINT32_C(0x7fffffff);
}
static int check(pointer_distance po,pointer_distance pn,scalar_distance so,scalar_distance sn,
                 uint32_t a,uint32_t b,uint32_t c,uint32_t d) {
    const uint32_t pair[] = {c,d};
    uint32_t expected = reference(a,b,c,d);
    ++cases;
    if (po(pair,a,b)==expected && pn(pair,a,b)==expected
            && so(a,b,c,d)==expected && sn(a,b,c,d)==expected) return 1;
    fprintf(stderr,"DISTANCE_MISMATCH a=%u b=%u c=%u d=%u expected=%u\n",a,b,c,d,expected);
    return 0;
}
int main(void) {
    if (sizeof(void *)!=8) return 2;
    long page = sysconf(_SC_PAGESIZE);
    size_t span = (0x121c000 + (size_t)page-1) & ~((size_t)page-1);
    unsigned char *engine = mmap(NULL,span,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    unsigned char *original = mmap(NULL,(size_t)page,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if (engine==MAP_FAILED || original==MAP_FAILED) return 3;
    memcpy(original,distance_pointer_original,sizeof(distance_pointer_original));
    memcpy(original+128,distance_scalar_original,sizeof(distance_scalar_original));
    memcpy(engine+0x121b860,distance_pointer_original,sizeof(distance_pointer_original));
    memcpy(engine+0x121b8bc,distance_scalar_original,sizeof(distance_scalar_original));
    __builtin___clear_cache((char *)original,(char *)original+page);
    if (mprotect(original,(size_t)page,PROT_READ|PROT_EXEC)) return 4;
    if (!install_saturated_distance((uintptr_t)engine) || !install_saturated_distance((uintptr_t)engine)) return 5;
    pointer_distance po=(pointer_distance)(void *)original,pn=(pointer_distance)(void *)(engine+0x121b860);
    scalar_distance so=(scalar_distance)(void *)(original+128),sn=(scalar_distance)(void *)(engine+0x121b8bc);
    const uint32_t edges[] = {0,1,2,32767,32768,46339,46340,46341,46342,65535,65536,
        0x7ffffffe,0x7fffffff,0x80000000,0x80000001,0xffff0000,0xffff4afa,0xffff4afb,
        0xffff4afc,0xffff7fff,0xfffffffe,0xffffffff};
    const size_t n=sizeof(edges)/sizeof(edges[0]);
    for(size_t a=0;a<n;++a)for(size_t b=0;b<n;++b)for(size_t c=0;c<n;++c)for(size_t d=0;d<n;++d)
        if(!check(po,pn,so,sn,edges[a],edges[b],edges[c],edges[d]))return 6;
    for(unsigned i=0;i<1000000;++i){
        uint32_t a=next_random(),b=next_random(),c=next_random(),d=next_random();
        if(!check(po,pn,so,sn,a,b,c,d))return 7;
        if(!check(po,pn,so,sn,a,b,a+(c%100001)-50000,b+(d%100001)-50000))return 8;
    }
    unsigned char *guard=mmap(NULL,(size_t)page*2,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
    if(guard==MAP_FAILED || mprotect(guard+page,(size_t)page,PROT_NONE))return 9;
    uint32_t *last=(uint32_t *)(void *)(guard+page-4);*last=0;
    /* Both original and replacement must avoid reading the protected y when x saturates. */
    if(po(last,46341,0)!=0x7fffffff || pn(last,46341,0)!=0x7fffffff)return 10;
    if(po(last,0x80000000,0)!=0x7fffffff || pn(last,0x80000000,0)!=0x7fffffff)return 11;
    printf("SATURATED_DISTANCE_CHECK cases=%" PRIu64 " original_and_candidate=equal conditional_y_read=preserved\n",cases);
    return 0;
}
