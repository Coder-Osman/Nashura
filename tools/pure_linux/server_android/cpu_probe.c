#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>
static uint64_t now_ns(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
}
__attribute__((noinline)) static uint64_t integer_kernel(uint64_t seed, int count) {
    uint64_t table[1024];
    for (int i=0;i<1024;++i) { seed=seed*6364136223846793005ULL+1; table[i]=seed; }
    for (int i=0;i<count;++i) {
        unsigned index=(unsigned)(seed>>32)&1023U;
        seed ^= table[index];
        seed=(seed<<17)|(seed>>47);
        if(seed&1) seed=seed*2862933555777941757ULL+3037000493ULL;
        else seed^=seed>>23;
        table[(index+37U)&1023U]=seed;
    }
    return seed;
}
__attribute__((noinline)) static double floating_kernel(double seed, int count) {
    double table[1024];
    for(int i=0;i<1024;++i) table[i]=(double)(i+1)*0.000123;
    for(int i=0;i<count;++i) {
        unsigned index=(unsigned)i&1023U;
        seed=seed*1.0000001+table[index];
        if(seed>50.0) seed=seed*0.03125;
        else seed+=0.000017;
        table[(index+37U)&1023U]=seed*0.001;
    }
    return seed;
}
int main(int argc,char **argv) {
    uint64_t seed=argc>1?strtoull(argv[1],0,10):1234567;
    for(int trial=0;trial<5;++trial) {
        uint64_t start=now_ns();
        uint64_t value=integer_kernel(seed,20000000);
        uint64_t end=now_ns();
        printf("kernel=integer trial=%d wall_ns=%llu digest=%016llx",trial,(unsigned long long)(end-start),(unsigned long long)value);putchar(10);
        start=now_ns();
        double number=floating_kernel((double)(seed%1000)*0.001,20000000);
        end=now_ns();
        printf("kernel=floating trial=%d wall_ns=%llu value=%.17g",trial,(unsigned long long)(end-start),number);putchar(10);
    }
    return 0;
}
