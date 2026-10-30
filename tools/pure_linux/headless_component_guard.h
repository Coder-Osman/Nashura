/* Pinned-build, serial Mainloop experiment. Bounds remain in the original
 * function. With flag bit zero, a null object or the exact guarded constant
 * type getter permits returning one without the frame and redundant reads.
 * Every other virtual target and flag-one call runs the original body.
 * Requires valid complete ordinary-memory objects; omitted reads, fault PCs,
 * signal/profiler stacks and concurrent mutation are outside the contract.
 * Include after headless_native_phases.h for its checked near-island allocator.
 */
#ifndef NASHURA_HEADLESS_COMPONENT_GUARD_H
#define NASHURA_HEADLESS_COMPONENT_GUARD_H

static const uint32_t component_guard_original[] = {
    0xaa0003e8,0x2a010049,0x2a1f03e0,0x37f805a9,0xb9400109,0x6b01013f,
    0x5400054d,0xb9400509,0x6b02013f,0x540004ed,0xa9bc7bfd,0xf9000bf7,
    0xa90257f6,0xa9034ff4,0x910003fd,0xaa0403f4,0x2a0303f3,0xaa0803f7,
    0x2a0103f6,0x2a0203f5,0xb40001e4,0xf9400288,0xaa1403e0,0xf9400908,
    0xd63f0100,0x7100141f,0x54000121,0xaa1403e0,0x97f6f90c,0x7100181f,
    0x540000a1,0xaa1403e0,0x97f6e6fd,0x97f0edb8,0xb5000120,0x36000113,
    0xf94026e0,0x2a1603e1,0x2a1503e2,0x97f368ff,0x36000060,0x2a1f03e0,
    0x14000002,0x52800020,0xa9434ff4,0xf9400bf7,0xa94257f6,0xa8c47bfd,
    0xd65f03c0
};
static const uint32_t component_guard_stub[] = {
    0x37000123,0xb40000c4,0xf9400089,0xf9400929,0x5800010a,0xeb0a013f,
    0x54000061,0x52800020,0xd65f03c0,0xa9bc7bfd,0x14000000,0xd503201f,
    0,0
};
static int install_component_guard(uintptr_t engine, int hot_getters) {
    static uintptr_t installed_engine, installed_island;
    static uint32_t installed_branch;
    const size_t bytes = sizeof(component_guard_original);
    uint32_t expected[sizeof(component_guard_original)/4];
    memcpy(expected, component_guard_original, bytes);
    if (hot_getters) {
        expected[28] = UINT32_C(0xb9411c00);
        expected[32] = UINT32_C(0xf9402400);
        expected[33] = UINT32_C(0xf9413000);
    }
    if (installed_engine) {
        if (installed_engine != engine) return 0;
        expected[10] = installed_branch;
    }
    if (memcmp((void *)(engine + 0x115d540), expected, bytes)) return 0;
    const struct { uintptr_t offset; uint32_t words[2]; } pure[] = {
        {0xf20210,{0x528000a0,0xd65f03c0}},
        {0xf1b9e0,{0xb9411c00,0xd65f03c0}},
        {0xf171b4,{0xf9402400,0xd65f03c0}},
        {0xd98ca4,{0xf9413000,0xd65f03c0}}
    };
    for (size_t i=0;i<sizeof(pure)/sizeof(pure[0]);++i)
        if (memcmp((void *)(engine+pure[i].offset),pure[i].words,8)) return 0;
    if (installed_engine) {
        uint32_t stub[sizeof(component_guard_stub)/4];
        memcpy(stub,component_guard_stub,sizeof(stub));
        if (!native_branch_word(installed_island+40,engine+0x115d56c,0x14000000,&stub[10])) return 0;
        uintptr_t target=engine+0xf20210; memcpy(&stub[12],&target,8);
        return memcmp((void *)installed_island,stub,sizeof(stub))==0;
    }
    long page=sysconf(_SC_PAGESIZE);
    if (page<4096 || (page&(page-1))) return 0;
    const struct native_phase_site site={0x115d568,0x14000000,0,0};
    uint32_t branch;
    void *island=native_phase_allocate_island(engine,&site,1,(size_t)page,&branch);
    if (island==MAP_FAILED) return 0;
    uint32_t stub[sizeof(component_guard_stub)/4];
    memcpy(stub,component_guard_stub,sizeof(stub));
    if (!native_branch_word((uintptr_t)island+40,engine+0x115d56c,0x14000000,&stub[10])) {
        munmap(island,(size_t)page); return 0;
    }
    uintptr_t target=engine+0xf20210; memcpy(&stub[12],&target,8);
    memcpy(island,stub,sizeof(stub));
    __builtin___clear_cache((char *)island,(char *)island+sizeof(stub));
    if (mprotect(island,(size_t)page,PROT_READ|PROT_EXEC)) {
        munmap(island,(size_t)page); return 0;
    }
    uintptr_t code_page=(engine+0x115d568)&~((uintptr_t)page-1);
    if (mprotect((void *)code_page,(size_t)page,PROT_READ|PROT_WRITE)) {
        munmap(island,(size_t)page); return 0;
    }
    memcpy((void *)(engine+0x115d568),&branch,4);
    __builtin___clear_cache((char *)engine+0x115d568,(char *)engine+0x115d56c);
    if (mprotect((void *)code_page,(size_t)page,PROT_READ|PROT_EXEC)) return 0;
    installed_engine=engine; installed_island=(uintptr_t)island; installed_branch=branch;
    printf("HEADLESS_NATIVE_OPTIMIZATION name=component-guard-v1 installed=1 functions=1 guarded_targets=1\n");
    fflush(stdout);
    return memcmp((void *)(engine+0x115d568),&branch,4)==0;
}
#endif
