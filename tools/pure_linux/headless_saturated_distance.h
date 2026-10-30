/* Fixed-build integer distance experiment; no floating point or cached state.
 * SUB remains 32-bit modular arithmetic. Signed widening squares sum to at most
 * 2^63; an unsigned comparison clamps at INT32_MAX without 64-bit overflow.
 * Pointer variant preserves the original conditional second-coordinate read.
 * Only AAPCS64 caller-saved temporaries/flags differ. Default remains original.
 */
#ifndef NASHURA_HEADLESS_SATURATED_DISTANCE_H
#define NASHURA_HEADLESS_SATURATED_DISTANCE_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static const uint32_t distance_pointer_original[] = {
    0xb9400008,0x1296a089,0x5292beeb,0x72bfffcb,0x4b080028,0x0b09010a,
    0x6b0b015f,0x54000062,0x12b00000,0xd65f03c0,0xb940040a,0x12b00000,
    0x4b0a004a,0x0b090149,0x6b0b013f,0x540000e3,0x1b087d08,0x1b0a7d49,
    0x5200790a,0x0b080128,0x6b0a013f,0x1a803100,0xd65f03c0
};
static const uint32_t distance_scalar_original[] = {
    0x4b020008,0x1296a08b,0x5292beea,0x72bfffca,0x0b0b0109,0x6b0a013f,
    0x54000062,0x12b00000,0xd65f03c0,0x4b030029,0x12b00000,0x0b0b012b,
    0x6b0a017f,0x540000e3,0x1b087d08,0x1b097d29,0x5200790a,0x0b080128,
    0x6b0a013f,0x1a803100,0xd65f03c0
};
static const uint32_t distance_pointer_replacement[] = {
    0xb9400008,0x4b080028,0x9b287d08,0x12b0000a,0xeb0a011f,0x54000102,
    0xb9400409,0x4b090049,0x9b297d29,0x8b090108,0xeb0a011f,0x1a8a3100,
    0xd65f03c0,0x2a0a03e0,0xd65f03c0
};
static const uint32_t distance_scalar_replacement[] = {
    0x4b020008,0x4b030029,0x9b287d08,0x9b297d29,0x8b090108,0x12b0000a,
    0xeb0a011f,0x1a8a3100,0xd65f03c0
};

static int install_saturated_distance(uintptr_t engine) {
    static int installed;
    const uintptr_t offsets[] = {0x121b860,0x121b8bc};
    const uint32_t *original[] = {distance_pointer_original,distance_scalar_original};
    const uint32_t *replacement[] = {distance_pointer_replacement,distance_scalar_replacement};
    const size_t original_sizes[] = {sizeof(distance_pointer_original),sizeof(distance_scalar_original)};
    const size_t replacement_sizes[] = {sizeof(distance_pointer_replacement),sizeof(distance_scalar_replacement)};
    for (size_t i = 0; i < 2; ++i) {
        if (installed) {
            if (memcmp((void *)(engine + offsets[i]),replacement[i],replacement_sizes[i])) return 0;
            for (size_t j = replacement_sizes[i]; j < original_sizes[i]; j += 4)
                if (*(const uint32_t *)(engine + offsets[i] + j) != UINT32_C(0xd503201f)) return 0;
        } else if (memcmp((void *)(engine + offsets[i]),original[i],original_sizes[i])) return 0;
    }
    if (installed) return 1;
    long size = sysconf(_SC_PAGESIZE);
    if (size < 4 || (size & (size - 1))) return 0;
    uintptr_t page = (engine + offsets[0]) & ~((uintptr_t)size - 1);
    if (((engine + offsets[1] + original_sizes[1] - 1) & ~((uintptr_t)size - 1)) != page) return 0;
    if (mprotect((void *)page,(size_t)size,PROT_READ | PROT_WRITE)) return 0;
    for (size_t i = 0; i < 2; ++i) {
        memcpy((void *)(engine + offsets[i]),replacement[i],replacement_sizes[i]);
        for (size_t j = replacement_sizes[i]; j < original_sizes[i]; j += 4)
            *(uint32_t *)(engine + offsets[i] + j) = UINT32_C(0xd503201f);
    }
    __builtin___clear_cache((char *)page,(char *)page + size);
    if (mprotect((void *)page,(size_t)size,PROT_READ | PROT_EXEC)) return 0;
    for (size_t i = 0; i < 2; ++i)
        if (memcmp((void *)(engine + offsets[i]),replacement[i],replacement_sizes[i])) return 0;
    installed = 1;
    printf("HEADLESS_NATIVE_OPTIMIZATION name=saturated-distance-v1 installed=1 functions=2\n");
    fflush(stdout);
    return 1;
}
#endif
